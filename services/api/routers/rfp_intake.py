"""Authenticated RFP PDF upload and asynchronous intake."""

from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from database import get_engine
from rfp_models import RFPTicket
from security import get_current_user
from tasks.rfp_intake import convert_rfp_pdf
from rfp_sse import (
    make_client,
    initial_cursor,
    stream_events,
    publish_rfp_created,
    validate_cursor,
)

router = APIRouter(prefix="/rfp", tags=["rfp"])

RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw"
MAX_BYTES = 15 * 1024 * 1024


@router.post("/intake", status_code=202)
async def upload_rfp(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """Store a PDF, persist its ticket and enqueue conversion."""
    if not getattr(user, "uuid", None):
        raise HTTPException(403, "Stable user identity required")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "A PDF file is required")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP intake requires PostgreSQL")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{uuid4()}.pdf"
    total = 0

    try:
        with path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, "PDF exceeds 15 MB")
                if total == len(chunk) and not chunk.startswith(b"%PDF-"):
                    raise HTTPException(415, "Invalid PDF signature")
                output.write(chunk)

        if total == 0:
            raise HTTPException(415, "Empty PDF")

        import pymupdf

        try:
            with pymupdf.open(str(path)) as document:
                if not document.is_pdf or document.page_count == 0:
                    raise ValueError("Invalid PDF")
        except (pymupdf.FileDataError, pymupdf.EmptyFileError, ValueError):
            raise HTTPException(415, "Invalid PDF structure")

        worker_pdf_path = str(path)
        ticket = RFPTicket(
            raw_pdf_path=worker_pdf_path,
            owner_uuid=user.uuid,
        )
        with Session(engine) as session:
            session.add(ticket)
            session.commit()
            ticket_id = ticket.ticket_id

        try:
            job = convert_rfp_pdf.apply_async(
                kwargs={
                    "ticket_id": ticket_id,
                    "pdf_path": worker_pdf_path,
                }
            )
        except Exception:
            try:
                with Session(engine) as session:
                    persisted = session.get(RFPTicket, ticket_id)
                    if persisted is not None:
                        session.delete(persisted)
                        session.commit()
            finally:
                path.unlink(missing_ok=True)
            raise HTTPException(503, "Unable to enqueue RFP")

        # Notify only after successful task dispatch.
        # Notification failures must not invalidate the RFP intake.
        try:
            await publish_rfp_created(ticket_id, "analizando", user.uuid)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "SSE publication unavailable; RFP intake preserved"
            )

        return {
            "ticket_id": ticket_id,
            "status": "analizando",
            "task_id": job.id,
        }

    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except (OSError, SQLAlchemyError):
        path.unlink(missing_ok=True)
        raise HTTPException(503, "RFP intake unavailable")
    finally:
        await file.close()


@router.get("/tickets")
def list_rfp_tickets(
    user=Depends(get_current_user),
):
    """List recent RFP tickets owned by the current user."""
    if not getattr(user, "uuid", None) or not user.is_active:
        raise HTTPException(403, "Stable active identity required")
    from sqlmodel import select
    from rfp_models import RFPMetadata

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        tickets = session.exec(
            select(RFPTicket)
            .where(RFPTicket.owner_uuid == user.uuid)
            .order_by(RFPTicket.created_at.desc())
            .limit(50)
        ).all()

        result = []
        for ticket in tickets:
            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket.ticket_id
                )
            ).one_or_none()

            result.append({
                "ticket_id": ticket.ticket_id,
                "status": ticket.status,
                "created_at": ticket.created_at.isoformat(),
                "client_name": metadata.client_name if metadata else None,
                "departments_needed": (
                    metadata.departments_needed if metadata else []
                ),
            })

    return {"tickets": result}


@router.get("/tickets/{ticket_id}")
def get_rfp_ticket(
    ticket_id: str,
    _user=Depends(get_current_user),
):
    """Read one ticket, metadata and department drafts."""
    from sqlmodel import select
    from rfp_models import RFPMetadata, RFPDepartmentSection

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "RFP ticket not found")

        from rfp_approval_auth import authorize_ticket_read
        authorize_ticket_read(_user, ticket)

        metadata = session.exec(
            select(RFPMetadata).where(
                RFPMetadata.ticket_id == ticket_id
            )
        ).one_or_none()

        sections = session.exec(
            select(RFPDepartmentSection).where(
                RFPDepartmentSection.ticket_id == ticket_id
            )
        ).all()

        return {
            "ticket_id": ticket.ticket_id,
            "status": ticket.status,
            "created_at": ticket.created_at.isoformat(),
            "client_name": metadata.client_name if metadata else None,
            "client_country": metadata.client_country if metadata else None,
            "departments_needed": (
                metadata.departments_needed if metadata else []
            ),
            "sections": [
                {
                    "department_id": section.department_id,
                    "key_aspects": section.key_aspects,
                    "draft_content": section.draft_content,
                    "evaluation_results": section.evaluation_results,
                    "approval_status": section.approval_status,
                }
                for section in sections
            ],
        }


@router.post("/tickets/{ticket_id}/generate", status_code=202)
def start_rfp_response_generation(
    ticket_id: str,
    user=Depends(get_current_user),
):
    """Reserve a completed RFP and dispatch its active departments."""
    from celery import group
    from sqlmodel import select
    from rfp_models import (
        RFPMetadata, RFPDepartmentSection, utcnow,
    )
    from tasks.rfp_intake import generate_rfp_response
    from data.pipelines.rfp_intake.rfp_orchestrator import (
        plan_departments,
    )

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None:
                raise HTTPException(404, "RFP ticket not found")

            from rfp_approval_auth import authorize_ticket_read
            authorize_ticket_read(user, ticket)

            if ticket.status != "analisis_completo":
                raise HTTPException(
                    409,
                    "Ticket is not available for response generation",
                )

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one_or_none()

            if metadata is None:
                raise HTTPException(409, "RFP metadata missing")

            plan = plan_departments(metadata.departments_needed)
            departments = [item["department_id"] for item in plan]

            sections = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id
                )
            ).all()

            by_department = {
                section.department_id: section
                for section in sections
            }

            if (
                len(sections) != len(departments)
                or set(by_department) != set(departments)
                or any(
                    not by_department[dept].key_aspects
                    or by_department[dept].approval_status is not None
                    or by_department[dept].evaluation_results.get(
                        "review_status"
                    )
                    for dept in departments
                )
            ):
                raise HTTPException(
                    409,
                    "Department handoff incomplete or already processed",
                )

            ticket.status = "generando_borrador"
            ticket.updated_at = utcnow()

    try:
        workflow = group(
            generate_rfp_response.s(ticket_id, department)
            for department in departments
        )
        job = workflow.apply_async()
    except Exception as exc:
        # Publication may have partially succeeded.
        # Keep the reservation: automatic retry could duplicate work.
        # Recovery requires checking broker and worker state first.
        raise HTTPException(
            503,
            "Dispatch uncertain; ticket remains reserved for recovery",
        ) from exc

    return {
        "ticket_id": ticket_id,
        "status": "generando_borrador",
        "departments": departments,
        "celery_group_id": job.id,
    }


from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class RFPApprovalDecisionRequest(BaseModel):
    """Human decision for an active RFP department."""

    decision: Literal["approve", "request_changes"]
    feedback: str | None = Field(default=None, max_length=4000)


class RFPDepartmentReviewResponse(BaseModel):
    """Strict whitelist of what a department approver may read."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    department_id: str
    ticket_status: str
    draft_content: str
    generation_status: str | None = None
    review_status: str | None = None
    approval_status: str | None = None


@router.get(
    "/tickets/{ticket_id}/departments/{department_id}/review",
    response_model=RFPDepartmentReviewResponse,
)
def get_department_review(
    ticket_id: str,
    department_id: str,
    response: Response,
    user=Depends(get_current_user),
):
    """Read-only draft of one department for its authorized approver."""
    from rfp_approval_auth import authorize_department_approval
    from rfp_approval_service import get_department_review as read_review

    authorize_department_approval(user, department_id)
    review = read_review(ticket_id, department_id)
    response.headers["Cache-Control"] = "no-store"
    return review


@router.post(
    "/tickets/{ticket_id}/departments/{department_id}/approval/start"
)
def start_department_approval(
    ticket_id: str,
    department_id: str,
    user=Depends(get_current_user),
):
    """Authorize and initiate the department review checkpoint."""
    from rfp_approval_auth import authorize_department_approval
    from rfp_approval_service import start_approval

    authorize_department_approval(user, department_id)
    return start_approval(ticket_id, department_id)


@router.post(
    "/tickets/{ticket_id}/departments/{department_id}/approval/decision"
)
def submit_department_approval(
    ticket_id: str,
    department_id: str,
    payload: RFPApprovalDecisionRequest,
    user=Depends(get_current_user),
):
    """Authorize, resume and persist the department decision."""
    from rfp_approval_auth import authorize_department_approval
    from rfp_approval_service import decide_approval

    authorize_department_approval(user, department_id)

    if payload.decision == "request_changes" and not (
        payload.feedback and payload.feedback.strip()
    ):
        raise HTTPException(
            status_code=422,
            detail="Feedback is required when requesting changes",
        )

    return decide_approval(
        ticket_id,
        department_id,
        payload.decision,
        payload.feedback,
    )



@router.post(
    "/tickets/{ticket_id}/departments/{department_id}/regenerate",
    status_code=202,
)
def regenerate_rfp_department(
    ticket_id: str,
    department_id: str,
    user=Depends(get_current_user),
):
    """Regenerate only a section returned for human corrections."""
    from sqlmodel import select
    from rfp_models import (
        RFPMetadata, RFPDepartmentSection, utcnow,
    )
    from rfp_approval_auth import authorize_department_approval
    from tasks.rfp_intake import generate_rfp_response

    authorize_department_approval(user, department_id)

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None:
                raise HTTPException(404, "RFP ticket not found")

            if ticket.status != "esperando_aprobación":
                raise HTTPException(
                    409, "Ticket is not awaiting approvals"
                )

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one_or_none()

            if (
                metadata is None
                or department_id not in (
                    metadata.departments_needed or []
                )
            ):
                raise HTTPException(404, "Inactive department")

            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == department_id,
                ).with_for_update()
            ).one_or_none()

            if section is None:
                raise HTTPException(404, "Department not found")

            if section.approval_status != "request_changes":
                raise HTTPException(
                    409, "Department has no requested corrections"
                )

            previous = dict(section.evaluation_results or {})
            feedback = previous.get("human_feedback")

            if not isinstance(feedback, str) or not feedback.strip():
                raise HTTPException(409, "Human feedback missing")

            revision = previous.get("approval_revision", 0)
            if type(revision) is not int or revision < 0:
                raise HTTPException(409, "Invalid revision state")

            history = list(previous.get("revision_history", []))
            history.append({
                "revision": revision,
                "review_status": previous.get("review_status"),
                "human_decision": previous.get("human_decision"),
                "human_feedback": feedback,
                "human_decided_at": previous.get("human_decided_at"),
                "draft_content": section.draft_content,
            })

            section.evaluation_results = {
                **previous,
                "revision_history": history,
                "approval_revision": revision + 1,
                "review_status": None,
                "generation_status": "queued",
            }
            section.draft_content = None
            section.approval_status = None
            section.approver = None
            section.approved_at = None

            ticket.updated_at = utcnow()
            session.add(section)
            session.add(ticket)

    try:
        job = generate_rfp_response.delay(
            ticket_id, department_id
        )
    except Exception as exc:
        # Keep the reservation to avoid unsafe duplicate dispatch.
        raise HTTPException(
            503,
            "Dispatch uncertain; inspect worker state before retry",
        ) from exc

    return {
        "ticket_id": ticket_id,
        "department_id": department_id,
        "status": "regeneration_queued",
        "celery_task_id": job.id,
    }




@router.post(
    "/tickets/{ticket_id}/departments/{department_id}/regenerate/recover",
    status_code=202,
)
def recover_rfp_department_dispatch(
    ticket_id: str,
    department_id: str,
    user=Depends(get_current_user),
):
    """Manually redispatch a queued revision without resetting approval."""
    from sqlmodel import select
    from rfp_models import RFPMetadata, RFPDepartmentSection
    from rfp_approval_auth import authorize_department_approval
    from tasks.rfp_intake import generate_rfp_response

    authorize_department_approval(user, department_id)

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None:
                raise HTTPException(404, "RFP ticket not found")

            if ticket.status != "esperando_aprobación":
                raise HTTPException(409, "Ticket is not awaiting approvals")

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one_or_none()

            if (
                metadata is None
                or department_id not in (metadata.departments_needed or [])
            ):
                raise HTTPException(404, "Inactive department")

            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == department_id,
                ).with_for_update()
            ).one_or_none()

            if section is None:
                raise HTTPException(404, "Department not found")

            evaluation = dict(section.evaluation_results or {})

            if (
                section.approval_status is not None
                or evaluation.get("generation_status") != "queued"
                or evaluation.get("review_status") is not None
            ):
                raise HTTPException(
                    409, "Only queued, unreviewed revisions can be recovered"
                )

            feedback = evaluation.get("human_feedback")
            revision = evaluation.get("approval_revision")

            if (
                not isinstance(feedback, str)
                or not feedback.strip()
                or type(revision) is not int
                or revision < 1
            ):
                raise HTTPException(409, "Invalid regeneration context")

            attempts = evaluation.get("dispatch_recovery_attempts", 0)

            if type(attempts) is not int or attempts < 0:
                raise HTTPException(409, "Invalid recovery counter")

            if attempts >= 3:
                raise HTTPException(
                    409, "Manual recovery limit reached"
                )

            evaluation["dispatch_recovery_attempts"] = attempts + 1
            section.evaluation_results = evaluation
            session.add(section)

    try:
        job = generate_rfp_response.delay(ticket_id, department_id)
    except Exception as exc:
        raise HTTPException(
            503,
            "Recovery dispatch uncertain; inspect PostgreSQL and worker",
        ) from exc

    return {
        "ticket_id": ticket_id,
        "department_id": department_id,
        "status": "regeneration_requeued",
        "celery_task_id": job.id,
    }



from math import isfinite
from pydantic import BaseModel, Field, StrictFloat, StrictInt


class RFPCommercialMetricConfirmation(BaseModel):
    value: StrictInt | StrictFloat
    source_reference: str = Field(min_length=8, max_length=160)


@router.post(
    "/tickets/{ticket_id}/departments/{department_id}/commercial-metrics"
)
def confirm_rfp_commercial_metric(
    ticket_id: str,
    department_id: str,
    payload: RFPCommercialMetricConfirmation,
    user=Depends(get_current_user),
):
    """Record an authorized human attestation of one commercial metric."""
    from sqlmodel import select
    from rfp_models import RFPMetadata, RFPDepartmentSection, utcnow
    from rfp_approval_auth import authorize_department_approval

    metric_by_department = {
        "warehouse": "monthly_capacity",
        "lastmile": "monthly_volume",
        "reverse": "returns_turnaround_hours",
    }

    approver = authorize_department_approval(user, department_id)
    metric = metric_by_department[department_id]
    value = payload.value
    source_reference = payload.source_reference.strip()

    if not source_reference:
        raise HTTPException(422, "Source reference is required")

    if value < 0 or (
        type(value) is float and not isfinite(value)
    ):
        raise HTTPException(422, "Invalid commercial metric")

    if metric == "returns_turnaround_hours" and value < 48:
        raise HTTPException(422, "Returns SLA cannot be below 48 hours")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None:
                raise HTTPException(404, "RFP ticket not found")

            if ticket.status != "esperando_aprobación":
                raise HTTPException(409, "Ticket is not awaiting approval")

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one_or_none()

            if (
                metadata is None
                or department_id not in (metadata.departments_needed or [])
            ):
                raise HTTPException(404, "Inactive department")

            section = session.exec(
                select(RFPDepartmentSection)
                .where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == department_id,
                )
                .with_for_update()
            ).one_or_none()

            if section is None:
                raise HTTPException(404, "Department section not found")

            evaluation = dict(section.evaluation_results or {})

            if (
                section.approval_status not in (None, "pending")
                or evaluation.get("generation_status") != "completed"
                or not evaluation.get("review_status")
            ):
                raise HTTPException(
                    409, "Department is not eligible for confirmation"
                )

            # Replace the complete attestation. Never carry over
            # previously confirmed values from another revision.
            evaluation["verified_commercial_metrics"] = {
                metric: value,
                "source_reference": source_reference,
                "confirmed_by": getattr(user, "email", ""),
                "confirmed_approver": approver,
                "confirmed_at": utcnow().isoformat(),
                "verification_method": "manual_department_attestation",
            }

            section.evaluation_results = evaluation
            session.add(section)

    return {
        "ticket_id": ticket_id,
        "department_id": department_id,
        "metric": metric,
        "value": value,
        "status": "confirmed",
    }


@router.post("/tickets/{ticket_id}/finalize")
def finalize_rfp_ticket(
    ticket_id: str,
    user=Depends(get_current_user),
):
    """Atomically persist the validated proposal and close its ticket."""
    from sqlmodel import select
    from rfp_models import (
        RFPTicket,
        RFPMetadata,
        RFPDepartmentSection,
        RFPFinalDocument,
        utcnow,
    )
    from rfp_lifecycle import transition_ticket
    from data.pipelines.rfp_intake.rfp_approval_graph import APPROVERS
    from data.pipelines.rfp_intake.rfp_final_projection import (
        build_from_persisted_records,
        project_section,
    )
    from data.pipelines.rfp_intake.rfp_final_document import (
        FinalDocumentBlocked,
    )
    from data.pipelines.rfp_intake.rfp_arbitration_graph import (
        build_arbitration_graph,
    )

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP closing requires PostgreSQL")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None:
                raise HTTPException(404, "RFP ticket not found")

            from rfp_approval_auth import authorize_ticket_read
            authorize_ticket_read(user, ticket)

            if ticket.status != "esperando_aprobación":
                raise HTTPException(
                    409, "Ticket is not eligible for finalization"
                )

            existing = session.get(RFPFinalDocument, ticket_id)
            if existing is not None:
                raise HTTPException(
                    409, "Final document already exists"
                )

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one_or_none()

            if metadata is None:
                raise HTTPException(409, "RFP metadata missing")

            sections = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id
                )
            ).all()

            active = list(metadata.departments_needed or [])
            by_department = {
                section.department_id: section
                for section in sections
            }

            for department in active:
                section = by_department.get(department)
                if (
                    section is None
                    or section.approver != APPROVERS.get(department)
                    or section.approved_at is None
                ):
                    raise HTTPException(
                        409,
                        f"Human approval not verified: {department}"
                    )

            try:
                document = build_from_persisted_records(
                    ticket, metadata, sections
                )

                structured = {
                    department: project_section(
                        by_department[department]
                    )
                    for department in active
                }

                arbitration = build_arbitration_graph().invoke({
                    "client_country": metadata.client_country,
                    "sections": structured,
                    "conflicts": [],
                    "arbitration_clear": False,
                })

                from agent_tracing import record_rfp_arbitration

                record_rfp_arbitration(
                    ticket_id,
                    active,
                    arbitration.get("conflicts", []),
                    arbitration["arbitration_clear"],
                )

                if not arbitration["arbitration_clear"]:
                    raise FinalDocumentBlocked(
                        "Arbitration conflicts remain unresolved"
                    )

            except FinalDocumentBlocked as exc:
                raise HTTPException(409, str(exc)) from exc

            lines = [
                "# Propuesta RFP",
                "",
                f"Ticket: {ticket.ticket_id}",
                f"Moneda: {document['currency']}",
            ]

            if metadata.client_name:
                lines.append(f"Cliente: {metadata.client_name}")

            lines.append("")

            for item in document["sections"]:
                lines.extend([
                    f"## {item['department_id']}",
                    "",
                    item["draft_content"],
                    "",
                    f"Aprobado por: {item['approver']}",
                    f"Fecha de aprobación: {item['approved_at']}",
                    "",
                ])

            final_content = "\n".join(lines)

            record = RFPFinalDocument(
                ticket_id=ticket_id,
                content=final_content,
                generated_at=utcnow(),
            )
            session.add(record)

            transition_ticket(ticket, "terminado")
            session.add(ticket)

            response = {
                "ticket_id": ticket_id,
                "status": "terminado",
                "download_url": (
                    f"/rfp/tickets/{ticket_id}/final/download"
                ),
            }

    return response


@router.get("/tickets/{ticket_id}/final/download")
def download_final_rfp_document(
    ticket_id: str,
    _user=Depends(get_current_user),
):
    """Download only a previously persisted final proposal."""
    import re
    from fastapi.responses import Response
    from rfp_models import RFPTicket, RFPFinalDocument

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP database unavailable")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        document = session.get(RFPFinalDocument, ticket_id)

        if (
            ticket is None
            or ticket.status != "terminado"
            or document is None
        ):
            raise HTTPException(
                404, "Final document not available"
            )

        from rfp_approval_auth import authorize_ticket_read
        authorize_ticket_read(_user, ticket)

        content = document.content

    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", ticket_id)

    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="rfp-{safe_id}.md"'
            )
        },
    )


@router.get("/events")
async def stream_rfp_notifications(
    request: Request,
    user=Depends(get_current_user),
):
    """Authenticated SSE stream for newly registered RFP tickets."""
    if not getattr(user, "is_active", False) or not getattr(user, "uuid", None):
        raise HTTPException(403, "Stable active identity required")
    try:
        requested = validate_cursor(
            request.headers.get("last-event-id")
        )
    except ValueError:
        raise HTTPException(400, "Invalid SSE event cursor")

    try:
        client = make_client()
        await client.ping()
        cursor = await initial_cursor(client, requested)
    except Exception:
        if "client" in locals():
            await client.aclose()
        raise HTTPException(503, "RFP notifications unavailable")

    return StreamingResponse(
        stream_events(client, request, cursor, user.uuid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
