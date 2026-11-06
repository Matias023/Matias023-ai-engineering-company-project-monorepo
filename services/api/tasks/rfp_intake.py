"""Asynchronous PDF conversion for RFP intake."""

from pathlib import Path
import sys

from celery_app import celery_app
from google.genai.errors import ServerError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from data.pipelines.rfp_intake.pdf_converter import convert_pdf_to_markdown


@celery_app.task(name="tasks.rfp_intake.convert_rfp_pdf")
def convert_rfp_pdf(ticket_id: str, pdf_path: str) -> dict:
    """Convert a previously stored PDF without changing ticket state."""
    raw_dir = (PROJECT_ROOT / "data/raw").resolve()
    path = Path(pdf_path).resolve()

    if not path.is_relative_to(raw_dir):
        raise ValueError("PDF path outside permitted storage")

    if path.suffix.lower() != ".pdf":
        raise ValueError("Only PDF files are supported")

    result = convert_pdf_to_markdown(path)

    markdown_path = path.with_suffix(".md")
    temporary_path = path.with_suffix(".md.tmp")

    try:
        temporary_path.write_text(
            result["markdown"],
            encoding="utf-8",
        )
        temporary_path.replace(markdown_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    if not result["readability_metrics"].get("readable"):
        return {
            "ticket_id": ticket_id,
            "markdown_path": str(markdown_path),
            "readability_metrics": result["readability_metrics"],
            "conversion_status": "manual_review_required",
            "classification_task_id": None,
        }

    classification_job = classify_rfp_document.apply_async(
        kwargs={
            "ticket_id": ticket_id,
            "markdown_path": str(markdown_path),
            "readability_metrics": result["readability_metrics"],
        },
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 1,
            "interval_step": 2,
            "interval_max": 5,
        },
    )

    return {
        "ticket_id": ticket_id,
        "markdown_path": str(markdown_path),
        "readability_metrics": result["readability_metrics"],
        "conversion_status": "converted",
        "classification_task_id": classification_job.id,
    }


@celery_app.task(name="tasks.rfp_intake.classify_rfp_document")
def classify_rfp_document(
    ticket_id: str,
    markdown_path: str,
    readability_metrics: dict,
) -> dict:
    """Classify converted Markdown and persist validated results."""
    import os

    api_dir = PROJECT_ROOT / "services" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from sqlmodel import Session

    from database import get_engine
    from rfp_models import RFPTicket
    from rfp_classification_service import persist_rfp_classification
    from data.pipelines.rfp_intake.rfp_classifier import classify_rfp

    raw_dir = (PROJECT_ROOT / "data/raw").resolve()
    path = Path(markdown_path).resolve()

    if not path.is_relative_to(raw_dir) or path.suffix != ".md":
        raise ValueError("Markdown path outside permitted storage")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("RFP classification requires PostgreSQL")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        if ticket is None:
            raise ValueError("RFP ticket not found")
        if ticket.status != "analizando":
            raise ValueError("RFP ticket is not analyzing")
        if Path(ticket.raw_pdf_path).resolve() != path.with_suffix(".pdf"):
            raise ValueError("Markdown does not belong to RFP ticket")

    if not readability_metrics.get("readable"):
        raise ValueError("PDF is not readable; manual review required")

    model = os.getenv("GEMINI_MODEL")
    if not model:
        raise RuntimeError("GEMINI_MODEL is not configured")

    markdown = path.read_text(encoding="utf-8")

    try:
        classification = classify_rfp(
            markdown,
            model=model,
        )
    except ValueError as exc:
        reason = str(exc)
        review_reasons = (
            "Unsupported extracted metadata:",
            "Unsupported department:",
            "Negated service requires review:",
            "Classification has no textual evidence",
            "Classification evidence not found in document",
        )
        if not reason.startswith(review_reasons):
            raise
        return {
            "ticket_id": ticket_id,
            "classification_status": "manual_review_required",
            "review_reason": reason,
            "document_type": None,
        }

    if classification.is_rfp and not classification.departments_needed:
        return {
            "ticket_id": ticket_id,
            "classification_status": "manual_review_required",
            "review_reason": "No departments identified for RFP",
            "document_type": classification.document_type,
        }

    persisted = persist_rfp_classification(
        ticket_id,
        classification,
        readability_metrics,
    )

    orchestration_task_id = None

    if classification.is_rfp:
        job = orchestrate_rfp_departments.apply_async(
            kwargs={
                "ticket_id": ticket_id,
                "markdown_path": str(path),
            },
            retry=True,
            retry_policy={
                "max_retries": 3,
                "interval_start": 1,
                "interval_step": 2,
                "interval_max": 5,
            },
        )
        orchestration_task_id = job.id

    return {
        **persisted,
        "document_type": classification.document_type,
        "classification_status": "classified",
        "orchestration_task_id": orchestration_task_id,
    }


@celery_app.task(
    name="tasks.rfp_intake.analyze_rfp_department",
    autoretry_for=(ServerError,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=2,
    retry_backoff_max=60,
    retry_jitter=True,
)
def analyze_rfp_department(
    ticket_id: str,
    department_id: str,
    markdown_path: str,
) -> dict:
    """Analyze an authorized department of a classified RFP."""
    import os

    api_dir = PROJECT_ROOT / "services" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from sqlmodel import Session, select
    from database import get_engine
    from rfp_models import RFPTicket, RFPMetadata
    from data.pipelines.rfp_intake.rfp_department_workers import (
        analyze_department,
    )

    raw_dir = (PROJECT_ROOT / "data/raw").resolve()
    path = Path(markdown_path).resolve()

    if not path.is_relative_to(raw_dir) or path.suffix != ".md":
        raise ValueError("Invalid RFP Markdown path")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL required")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        if ticket is None or ticket.status != "analizando":
            raise ValueError("Ticket not available for analysis")

        if Path(ticket.raw_pdf_path).resolve() != path.with_suffix(".pdf"):
            raise ValueError("Document does not belong to ticket")

        metadata = session.exec(
            select(RFPMetadata).where(
                RFPMetadata.ticket_id == ticket_id
            )
        ).one_or_none()

        if metadata is None:
            raise ValueError("RFP metadata not found")

        if department_id not in metadata.departments_needed:
            raise ValueError("Department not requested")

    model = os.getenv("GEMINI_MODEL")
    if not model:
        raise RuntimeError("GEMINI_MODEL is not configured")

    result = analyze_department(
        department_id,
        path.read_text(encoding="utf-8"),
        model=model,
    )

    return {"ticket_id": ticket_id, **result}


@celery_app.task(name="tasks.rfp_intake.synthesize_rfp_departments")
def synthesize_rfp_departments(
    department_results: list[dict],
    ticket_id: str,
) -> dict:
    """Validate and persist a completed set of departmental analyses."""
    api_dir = PROJECT_ROOT / "services" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from sqlmodel import Session, select
    from database import get_engine
    from rfp_models import (
        RFPTicket, RFPMetadata, RFPDepartmentSection, utcnow,
    )
    from data.pipelines.rfp_intake.rfp_synthesizer import (
        synthesize_departments,
    )

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL required")

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()
            if ticket is None or ticket.status != "analizando":
                raise ValueError("Ticket not available for synthesis")

            metadata = session.exec(
                select(RFPMetadata)
                .where(RFPMetadata.ticket_id == ticket_id)
            ).one_or_none()
            if metadata is None:
                raise ValueError("RFP metadata not found")

            consolidated = synthesize_departments(
                metadata.departments_needed,
                department_results,
            )

            if any(
                result.get("ticket_id") != ticket_id
                for result in department_results
            ):
                raise ValueError("Department result ticket mismatch")

            existing = session.exec(
                select(RFPDepartmentSection)
                .where(RFPDepartmentSection.ticket_id == ticket_id)
            ).first()
            if existing is not None:
                raise ValueError("Department sections already persisted")

            # Parte 1: habilitar handoff solo tras síntesis validada.
            ticket.status = "analisis_completo"
            ticket.updated_at = utcnow()

            for section in consolidated["sections"]:
                session.add(RFPDepartmentSection(
                    ticket_id=ticket_id,
                    department_id=section["department_id"],
                    key_aspects=section["key_aspects"],
                    draft_content=section["draft_content"],
                    evaluation_results={
                        "evidence": section["evidence"],
                        "unconfirmed": section["unconfirmed"],
                    },
                ))

    return {
        "ticket_id": ticket_id,
        "synthesis_status": "ready_for_review",
        "departments_completed": consolidated["departments_completed"],
    }


@celery_app.task(name="tasks.rfp_intake.orchestrate_rfp_departments")
def orchestrate_rfp_departments(ticket_id: str, markdown_path: str) -> dict:
    """Dispatch authorized department analyses as a Celery chord."""
    from celery import chord
    from sqlmodel import Session, select

    api_dir = PROJECT_ROOT / "services" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from database import get_engine
    from rfp_models import RFPTicket, RFPMetadata
    from data.pipelines.rfp_intake.rfp_orchestrator import plan_departments

    path = Path(markdown_path).resolve()
    raw_dir = (PROJECT_ROOT / "data/raw").resolve()
    if not path.is_relative_to(raw_dir) or path.suffix != ".md":
        raise ValueError("Invalid Markdown path")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL required")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        if ticket is None or ticket.status != "analizando":
            raise ValueError("Ticket not available for orchestration")
        if Path(ticket.raw_pdf_path).resolve() != path.with_suffix(".pdf"):
            raise ValueError("Document does not belong to ticket")

        metadata = session.exec(
            select(RFPMetadata).where(RFPMetadata.ticket_id == ticket_id)
        ).one_or_none()
        if metadata is None:
            raise ValueError("RFP metadata not found")

        plan = plan_departments(metadata.departments_needed)

    if not path.is_file():
        raise ValueError("Markdown file not found")

    header = [
        analyze_rfp_department.s(
            ticket_id=ticket_id,
            department_id=item["department_id"],
            markdown_path=str(path),
        )
        for item in plan
    ]
    callback = synthesize_rfp_departments.s(ticket_id=ticket_id)
    workflow = chord(header)(callback)

    return {
        "ticket_id": ticket_id,
        "orchestration_status": "dispatched",
        "departments": [item["department_id"] for item in plan],
        "synthesis_task_id": workflow.id,
    }


@celery_app.task(name="tasks.rfp_intake.generate_rfp_response")
def generate_rfp_response(ticket_id: str, department_id: str) -> dict:
    """Run Part 2 generator-evaluator cycle for one RFP department."""
    api_dir = PROJECT_ROOT / "services" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

    from sqlmodel import Session, select
    from database import get_engine
    from rfp_models import (
        RFPTicket, RFPMetadata, RFPDepartmentSection, utcnow,
    )
    from data.pipelines.rfp_intake.rfp_response_cycle import (
        run_department_review,
    )
    from data.pipelines.rfp_intake.rfp_orchestrator import (
        plan_departments,
    )

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("RFP response generation requires PostgreSQL")

    with Session(engine) as session:
        ticket = session.get(RFPTicket, ticket_id)
        if ticket is None:
            raise ValueError("RFP ticket not found")

        if ticket.status not in (
            "analisis_completo",
            "generando_borrador",
            "en_evaluación",
            "esperando_aprobación",
        ):
            raise ValueError("Ticket not available for response generation")

        metadata = session.exec(
            select(RFPMetadata).where(
                RFPMetadata.ticket_id == ticket_id
            )
        ).one_or_none()

        if metadata is None:
            raise ValueError("RFP metadata not found")

        allowed = {
            item["department_id"]
            for item in plan_departments(metadata.departments_needed)
        }
        if department_id not in allowed:
            raise ValueError("Department not requested")

        section = session.exec(
            select(RFPDepartmentSection).where(
                RFPDepartmentSection.ticket_id == ticket_id,
                RFPDepartmentSection.department_id == department_id,
            ).with_for_update()
        ).one_or_none()

        if section is None:
            raise ValueError("Department analysis not found")

        if section.approval_status is not None:
            raise ValueError("Department approval already started")

        if section.evaluation_results.get("review_status"):
            raise ValueError("Department response already evaluated")

        if section.evaluation_results.get("generation_status") == "running":
            raise ValueError("Department generation already running")

        aspects = list(section.key_aspects)

        active_evaluation = dict(section.evaluation_results)
        active_evaluation.pop("verified_commercial_metrics", None)

        section.evaluation_results = {
            **active_evaluation,
            "generation_status": "running",
        }
        session.add(section)
        session.commit()

        initial_feedback = (
            section.evaluation_results or {}
        ).get("human_feedback")

        shared_metadata = {
            "client_name": metadata.client_name,
            "client_country": metadata.client_country,
            "services_requested": metadata.services_requested,
            "monthly_volume": metadata.monthly_volume,
            "deadline": metadata.deadline,
            "budget_range": metadata.budget_range,
        }

    try:
        result = run_department_review(
            department_id,
            aspects,
            shared_metadata,
            initial_feedback=initial_feedback,
        )
    except Exception as exc:
        # Persist failure without automatically retrying Gemini.
        # Never overwrite a section already finalized or reassigned.
        try:
            with Session(engine) as failure_session:
                with failure_session.begin():
                    failed_section = failure_session.exec(
                        select(RFPDepartmentSection).where(
                            RFPDepartmentSection.ticket_id == ticket_id,
                            RFPDepartmentSection.department_id == department_id,
                        ).with_for_update()
                    ).one_or_none()

                    if (
                        failed_section is not None
                        and failed_section.approval_status is None
                        and not failed_section.evaluation_results.get(
                            "review_status"
                        )
                        and failed_section.evaluation_results.get(
                            "generation_status"
                        ) == "running"
                    ):
                        failed_section.evaluation_results = {
                            **failed_section.evaluation_results,
                            "generation_status": "failed",
                            "generation_error_type": type(exc).__name__,
                        }
                        failure_session.add(failed_section)
        except Exception:
            # Preserve the original generation exception.
            # Recovery must inspect PostgreSQL before any retry.
            raise RuntimeError(
                "Generation failed and failure persistence "
                "could not be confirmed"
            ) from exc

        raise

    with Session(engine) as session:
        with session.begin():
            ticket = session.exec(
                select(RFPTicket)
                .where(RFPTicket.ticket_id == ticket_id)
                .with_for_update()
            ).one_or_none()

            if ticket is None or ticket.status not in (
                "analisis_completo",
                "generando_borrador",
                "en_evaluación",
            "esperando_aprobación",
            ):
                raise ValueError("Ticket state changed during generation")

            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == department_id,
                ).with_for_update()
            ).one_or_none()

            if section is None:
                raise ValueError("Department section disappeared")

            if section.approval_status is not None:
                raise ValueError("Department approval already started")

            if section.evaluation_results.get("review_status"):
                raise ValueError("Department response already evaluated")

            if section.evaluation_results.get("generation_status") != "running":
                raise ValueError("Department generation reservation lost")

            previous = dict(section.evaluation_results)
            previous.pop("verified_commercial_metrics", None)

            generated_evaluation = dict(result["evaluation_results"])
            generated_evaluation.pop("verified_commercial_metrics", None)

            section.draft_content = result["draft_content"]
            section.evaluation_results = {
                **previous,
                **generated_evaluation,
                "generation_status": "completed",
                "review_status": result["review_status"],
                "iterations": result["iterations"],
                "missing_information": result["missing_information"],
                "currency": result["currency"],
            }

            sections = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id
                )
            ).all()

            completed = all(
                item.evaluation_results.get("review_status")
                or item.id == section.id
                for item in sections
            )

            if ticket.status == "esperando_aprobación":
                # Preserve independent department approvals.
                pass
            elif completed:
                any_failed = any(
                    (
                        result["review_status"]
                        if item.id == section.id
                        else item.evaluation_results.get("review_status")
                    ) == "needs_human_review"
                    for item in sections
                )
                ticket.status = (
                    "needs_human_review"
                    if any_failed else "en_evaluación"
                )
            else:
                ticket.status = "generando_borrador"

            ticket.updated_at = utcnow()

    # Persist metadata-only tracing after the PostgreSQL commit.
    # Logging must not turn a committed generation into a failed task.
    try:
        from agent_tracing import record_rfp_review

        record_rfp_review(
            ticket_id,
            department_id,
            result,
            key_aspect_count=len(aspects),
            had_human_feedback=bool(initial_feedback),
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "RFP review trace unavailable after database commit"
        )

    return {
        "ticket_id": ticket_id,
        "department_id": department_id,
        "review_status": result["review_status"],
        "iterations": result["iterations"],
    }
