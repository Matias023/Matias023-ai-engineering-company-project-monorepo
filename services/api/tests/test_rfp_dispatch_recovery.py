"""Controlled RFP regeneration dispatch recovery regression."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

import database
import rfp_approval_auth as authorization
import routers.rfp_intake as router

from rfp_models import RFPTicket, RFPMetadata, RFPDepartmentSection
from tasks.rfp_intake import generate_rfp_response


def test_uncertain_dispatch_can_be_recovered_without_resetting_review():
    import subprocess
    from sqlalchemy.engine import URL

    result = subprocess.run(
        [
            "docker", "exec", "trackflow-rfp-pg-demo",
            "sh", "-c",
            'printf "%s\\n" "$POSTGRES_USER" "$POSTGRES_PASSWORD"',
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    credentials = result.stdout.splitlines()
    if len(credentials) != 2 or not all(credentials):
        raise RuntimeError("SAFE ABORT: credenciales locales inválidas")

    username, password = credentials

    url = URL.create(
        "postgresql+psycopg2",
        username=username,
        password=password,
        host="127.0.0.1",
        port=55433,
        database="rfp_regression_test",
    )

    engine = create_engine(url, connect_args={"connect_timeout": 5})
    ticket_id = str(uuid.uuid4())
    rfp_id = str(uuid.uuid4())

    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT current_database()")
            ).scalar_one() == "rfp_regression_test"

        with Session(engine) as session:
            with session.begin():
                session.add(RFPTicket(
                    ticket_id=ticket_id,
                    rfp_id=rfp_id,
                    status="esperando_aprobación",
                    raw_pdf_path="test-only/dispatch-recovery.pdf",
                ))
                session.flush()

                session.add(RFPMetadata(
                    rfp_id=rfp_id,
                    ticket_id=ticket_id,
                    client_name="SYNTHETIC RECOVERY CLIENT",
                    client_country="Spain",
                    services_requested=["reverse"],
                    departments_needed=["reverse"],
                ))

                session.add(RFPDepartmentSection(
                    ticket_id=ticket_id,
                    department_id="reverse",
                    key_aspects=["Synthetic returns"],
                    draft_content="OLD SYNTHETIC DRAFT",
                    approval_status="request_changes",
                    evaluation_results={
                        "generation_status": "completed",
                        "review_status": "needs_human_review",
                        "approval_revision": 0,
                        "human_decision": "request_changes",
                        "human_feedback": "Correct the synthetic proposal",
                    },
                ))

        dispatch_result = SimpleNamespace(id="synthetic-recovered-task")

        with (
            patch.object(router, "get_engine", return_value=engine),
            patch.object(authorization, "authorize_department_approval"),
            patch.object(
                generate_rfp_response,
                "delay",
                side_effect=[
                    RuntimeError("synthetic broker uncertainty"),
                    dispatch_result,
                ],
            ) as dispatch,
        ):
            try:
                router.regenerate_rfp_department(
                    ticket_id, "reverse", user=object()
                )
            except HTTPException as exc:
                assert exc.status_code == 503
            else:
                raise AssertionError("Synthetic dispatch failure was ignored")

            with Session(engine) as session:
                section = session.exec(
                    select(RFPDepartmentSection).where(
                        RFPDepartmentSection.ticket_id == ticket_id,
                        RFPDepartmentSection.department_id == "reverse",
                    )
                ).one()

                assert section.approval_status is None
                assert section.draft_content is None
                assert section.evaluation_results["revision_history"][-1][
                    "draft_content"
                ] == "OLD SYNTHETIC DRAFT"
                assert section.evaluation_results["generation_status"] == "queued"
                assert section.evaluation_results["approval_revision"] == 1

            recovered = router.recover_rfp_department_dispatch(
                ticket_id, "reverse", user=object()
            )

            assert recovered["status"] == "regeneration_requeued"
            assert recovered["celery_task_id"] == dispatch_result.id
            assert dispatch.call_count == 2

            with Session(engine) as session:
                section = session.exec(
                    select(RFPDepartmentSection).where(
                        RFPDepartmentSection.ticket_id == ticket_id,
                        RFPDepartmentSection.department_id == "reverse",
                    )
                ).one()

                assert section.evaluation_results[
                    "dispatch_recovery_attempts"
                ] == 1
                assert section.evaluation_results[
                    "generation_status"
                ] == "queued"

                section.evaluation_results = {
                    **section.evaluation_results,
                    "generation_status": "running",
                }
                session.add(section)
                session.commit()

            try:
                router.recover_rfp_department_dispatch(
                    ticket_id, "reverse", user=object()
                )
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("Running task was redispatched")

            assert dispatch.call_count == 2

    finally:
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT current_database()")
                ).scalar_one() == "rfp_regression_test"

            with Session(engine) as session:
                with session.begin():
                    for model in (RFPDepartmentSection, RFPMetadata):
                        records = session.exec(
                            select(model).where(
                                model.ticket_id == ticket_id
                            )
                        ).all()
                        for record in records:
                            session.delete(record)
                        session.flush()

                    ticket = session.get(RFPTicket, ticket_id)
                    if ticket is not None:
                        session.delete(ticket)
        finally:
            engine.dispose()
