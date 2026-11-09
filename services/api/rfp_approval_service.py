"""Persistence-backed human approval service for TrackFlow RFPs."""

import os
from pathlib import Path

from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlmodel import Session, select

from database import get_engine
from rfp_lifecycle import transition_ticket
from rfp_models import (
    RFPTicket,
    RFPMetadata,
    RFPDepartmentSection,
    utcnow,
)
from data.pipelines.rfp_intake.rfp_approval_graph import (
    APPROVERS,
    approval_thread_id,
    build_approval_graph,
)


def _checkpoint_path() -> Path:
    path = Path(os.getenv(
        "RFP_APPROVAL_CHECKPOINT_DB",
        "/app/data/rfp-approval-checkpoints.sqlite",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _config(
    ticket_id: str,
    department_id: str,
    section,
) -> dict:
    revision = (section.evaluation_results or {}).get(
        "approval_revision", 0
    )
    return {
        "configurable": {
            "thread_id": approval_thread_id(
                ticket_id, department_id, revision
            )
        }
    }


def _load_active_section(session, ticket_id, department_id):
    ticket = session.exec(
        select(RFPTicket)
        .where(RFPTicket.ticket_id == ticket_id)
        .with_for_update()
    ).one_or_none()

    if ticket is None:
        raise HTTPException(404, "RFP ticket not found")

    metadata = session.exec(
        select(RFPMetadata).where(RFPMetadata.ticket_id == ticket_id)
    ).one_or_none()

    if metadata is None or department_id not in metadata.departments_needed:
        raise HTTPException(404, "Department is not active for this RFP")

    section = session.exec(
        select(RFPDepartmentSection).where(
            RFPDepartmentSection.ticket_id == ticket_id,
            RFPDepartmentSection.department_id == department_id,
        )
    ).one_or_none()

    if section is None or not section.draft_content:
        raise HTTPException(409, "Department draft is not ready")

    return ticket, section


REVIEWABLE_TICKET_STATUSES = frozenset({
    "needs_human_review",
    "esperando_aprobación",
})


def load_department_review(session, ticket_id: str, department_id: str) -> dict:
    """Read-only projection of one department draft for its approver.

    Never locks rows, commits or mutates state. Only the whitelisted
    fields below leave this function.
    """
    ticket = session.get(RFPTicket, ticket_id)

    if ticket is None:
        raise HTTPException(404, "RFP ticket not found")

    metadata = session.exec(
        select(RFPMetadata).where(RFPMetadata.ticket_id == ticket_id)
    ).one_or_none()

    if metadata is None or department_id not in (
        metadata.departments_needed or []
    ):
        raise HTTPException(404, "Department is not active for this RFP")

    if ticket.status not in REVIEWABLE_TICKET_STATUSES:
        raise HTTPException(409, "Ticket is not in human review")

    section = session.exec(
        select(RFPDepartmentSection).where(
            RFPDepartmentSection.ticket_id == ticket_id,
            RFPDepartmentSection.department_id == department_id,
        )
    ).one_or_none()

    if section is None or not section.draft_content:
        raise HTTPException(409, "Department draft is not ready")

    evaluation = section.evaluation_results or {}

    return {
        "ticket_id": ticket.ticket_id,
        "department_id": section.department_id,
        "ticket_status": ticket.status,
        "draft_content": section.draft_content,
        "generation_status": evaluation.get("generation_status"),
        "review_status": evaluation.get("review_status"),
        "approval_status": section.approval_status,
    }


def get_department_review(ticket_id: str, department_id: str) -> dict:
    """Return the reviewable draft of one active department."""

    if department_id not in APPROVERS:
        raise HTTPException(404, "Unknown department")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP approval requires PostgreSQL")

    with Session(engine) as session:
        return load_department_review(session, ticket_id, department_id)


def start_approval(ticket_id: str, department_id: str) -> dict:
    """Pause one active department for its human reviewer."""

    if department_id not in APPROVERS:
        raise HTTPException(404, "Unknown department")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP approval requires PostgreSQL")

    with Session(engine) as session:
        with session.begin():
            ticket, section = _load_active_section(
                session, ticket_id, department_id
            )

            if ticket.status not in {
                "en_evaluación",
                "needs_human_review",
                "esperando_aprobación",
            }:
                raise HTTPException(409, "Ticket is not ready for approval")

            metadata = session.exec(
                select(RFPMetadata).where(
                    RFPMetadata.ticket_id == ticket_id
                )
            ).one()

            active = set(metadata.departments_needed or [])

            sections = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id
                )
            ).all()

            ready = {
                item.department_id
                for item in sections
                if item.draft_content
                and (item.evaluation_results or {}).get("review_status")
            }

            if not active or not active.issubset(ready):
                raise HTTPException(
                    409,
                    "All active departments must finish evaluation "
                    "before human approval",
                )

            if section.approval_status not in {None, "pending"}:
                raise HTTPException(409, "Department already decided")

            with SqliteSaver.from_conn_string(
                str(_checkpoint_path())
            ) as saver:
                graph = build_approval_graph(saver)
                config = _config(ticket_id, department_id, section)
                snapshot = graph.get_state(config)

                if not snapshot.next:
                    if snapshot.values:
                        raise HTTPException(
                            409, "Approval checkpoint already completed"
                        )

                    result = graph.invoke({
                        "ticket_id": ticket_id,
                        "department_id": department_id,
                        "draft_content": section.draft_content,
                        "decision": None,
                        "feedback": None,
                        "approver": None,
                    }, config)

                    if not result.get("__interrupt__"):
                        raise RuntimeError("Approval graph did not interrupt")

            if ticket.status in {
                "en_evaluación", "needs_human_review"
            }:
                transition_ticket(ticket, "esperando_aprobación")

            section.approval_status = "pending"
            session.add(section)
            session.add(ticket)

    return {
        "ticket_id": ticket_id,
        "department_id": department_id,
        "approval_status": "pending",
        "approver": APPROVERS[department_id],
    }


def decide_approval(
    ticket_id: str,
    department_id: str,
    decision: str,
    feedback: str | None,
) -> dict:
    """Resume the existing checkpoint and persist the human decision."""

    if decision not in {"approve", "request_changes"}:
        raise HTTPException(422, "Invalid decision")

    if decision == "request_changes" and not (
        isinstance(feedback, str) and feedback.strip()
    ):
        raise HTTPException(422, "Feedback is required")

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        raise HTTPException(503, "RFP approval requires PostgreSQL")

    with Session(engine) as session:
        with session.begin():
            ticket, section = _load_active_section(
                session, ticket_id, department_id
            )

            if (
                ticket.status != "esperando_aprobación"
                or section.approval_status != "pending"
            ):
                raise HTTPException(409, "Department is not awaiting a decision")

            with SqliteSaver.from_conn_string(
                str(_checkpoint_path())
            ) as saver:
                graph = build_approval_graph(saver)
                config = _config(ticket_id, department_id, section)

                snapshot = graph.get_state(config)
                saved = dict(snapshot.values or {})

                if (
                    saved.get("ticket_id") != ticket_id
                    or saved.get("department_id") != department_id
                    or saved.get("draft_content") != section.draft_content
                ):
                    raise HTTPException(
                        409, "Approval checkpoint does not match current draft"
                    )

                if snapshot.next:
                    result = graph.invoke(
                        Command(resume={
                            "decision": decision,
                            "approver": APPROVERS[department_id],
                            "feedback": feedback,
                        }),
                        config,
                    )
                else:
                    if (
                        saved.get("decision") not in {
                            "approve", "request_changes"
                        }
                        or saved.get("approver") != APPROVERS[department_id]
                    ):
                        raise HTTPException(
                            409, "Completed checkpoint is not recoverable"
                        )

                    if saved["decision"] != decision:
                        raise HTTPException(
                            409, "Approval decision conflicts with checkpoint"
                        )

                    if decision == "request_changes":
                        saved_feedback = saved.get("feedback")
                        if (
                            not isinstance(saved_feedback, str)
                            or saved_feedback.strip() != feedback.strip()
                        ):
                            raise HTTPException(
                                409, "Approval feedback conflicts with checkpoint"
                            )

                    result = saved

            if result.get("decision") != decision:
                raise RuntimeError("Approval resume returned an invalid decision")

            section.approval_status = (
                "approved" if decision == "approve"
                else "request_changes"
            )
            section.approver = APPROVERS[department_id]
            section.approved_at = (
                utcnow() if decision == "approve" else None
            )

            evaluations = dict(section.evaluation_results or {})
            evaluations["human_decision"] = decision
            evaluations["human_approver"] = APPROVERS[department_id]
            evaluations["human_decided_at"] = utcnow().isoformat()

            if decision == "request_changes":
                evaluations["human_feedback"] = feedback.strip()

            section.evaluation_results = evaluations

            ticket.updated_at = utcnow()
            session.add(section)
            session.add(ticket)

            response = {
                "ticket_id": ticket_id,
                "department_id": department_id,
                "approval_status": section.approval_status,
                "approver": section.approver,
                "requires_regeneration": decision == "request_changes",
            }

    return response
