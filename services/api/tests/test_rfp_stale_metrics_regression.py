"""Regression: regenerated RFPs must not reuse old verified metrics."""

import uuid
from unittest.mock import patch

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

import database
import data.pipelines.rfp_intake.rfp_response_cycle as cycle

from rfp_models import (
    RFPTicket,
    RFPMetadata,
    RFPDepartmentSection,
)


def test_regeneration_invalidates_stale_commercial_metrics():
    """Exercise the Celery task directly against isolated PostgreSQL."""

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
        with engine.connect() as conn:
            database_name = conn.execute(
                text("SELECT current_database()")
            ).scalar_one()

        assert database_name == "rfp_regression_test"

        with Session(engine) as session:
            with session.begin():
                session.add(RFPTicket(
                    ticket_id=ticket_id,
                    rfp_id=rfp_id,
                    status="esperando_aprobación",
                    raw_pdf_path="test-only/regression.pdf",
                ))

                session.flush()

                session.add(RFPMetadata(
                    rfp_id=rfp_id,
                    ticket_id=ticket_id,
                    client_name="SYNTHETIC TEST CLIENT",
                    client_country="Spain",
                    services_requested=["reverse"],
                    departments_needed=["reverse"],
                ))

                session.add(RFPDepartmentSection(
                    ticket_id=ticket_id,
                    department_id="reverse",
                    key_aspects=["Returns"],
                    draft_content=None,
                    approval_status=None,
                    evaluation_results={
                        "generation_status": "queued",
                        "review_status": None,
                        "approval_revision": 1,
                        "human_feedback": "Revise the proposal",
                        "verified_commercial_metrics": {
                            "returns_turnaround_hours": 48,
                        },
                    },
                ))

        observed = []

        def fake_review(*args, **kwargs):
            assert kwargs["initial_feedback"] == (
                "Revise the proposal"
            )

            with Session(engine) as session:
                section = session.exec(
                    select(RFPDepartmentSection).where(
                        RFPDepartmentSection.ticket_id == ticket_id,
                        RFPDepartmentSection.department_id == "reverse",
                    )
                ).one()

                observed.append(
                    "verified_commercial_metrics"
                    not in section.evaluation_results
                )

            return {
                "draft_content": "SYNTHETIC REGENERATED DRAFT",
                "evaluation_results": {
                    "test_marker": "new-evaluation",
                    "verified_commercial_metrics": {
                        "returns_turnaround_hours": 24,
                    },
                },
                "review_status": "needs_human_review",
                "iterations": 1,
                "missing_information": [],
                "currency": "EUR",
            }

        from tasks.rfp_intake import generate_rfp_response

        with (
            patch.object(
                database,
                "get_engine",
                return_value=engine,
            ),
            patch.object(
                cycle,
                "run_department_review",
                side_effect=fake_review,
            ),
        ):
            generate_rfp_response.run(ticket_id, "reverse")

        assert observed == [True], (
            "Old metrics survived at generation start"
        )

        with Session(engine) as session:
            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == "reverse",
                )
            ).one()

            evaluation = section.evaluation_results

            assert section.draft_content == (
                "SYNTHETIC REGENERATED DRAFT"
            )
            assert evaluation["generation_status"] == "completed"
            assert evaluation["test_marker"] == "new-evaluation"

            assert "verified_commercial_metrics" not in evaluation, (
                "Unverified or stale metrics survived regeneration"
            )

            assert evaluation["human_feedback"] == (
                "Revise the proposal"
            )
            assert evaluation["approval_revision"] == 1

            ticket = session.get(RFPTicket, ticket_id)
            assert ticket.status == "esperando_aprobación"

    finally:
        try:
            with engine.connect() as conn:
                database_name = conn.execute(
                    text("SELECT current_database()")
                ).scalar_one()

            if database_name != "rfp_regression_test":
                raise RuntimeError(
                    "Cleanup blocked: unexpected database"
                )

            with Session(engine) as session:
                with session.begin():
                    sections = session.exec(
                        select(RFPDepartmentSection).where(
                            RFPDepartmentSection.ticket_id == ticket_id
                        )
                    ).all()

                    for section in sections:
                        session.delete(section)

                    session.flush()

                    metadata = session.exec(
                        select(RFPMetadata).where(
                            RFPMetadata.ticket_id == ticket_id
                        )
                    ).all()

                    for item in metadata:
                        session.delete(item)

                    session.flush()

                    ticket = session.get(RFPTicket, ticket_id)

                    if ticket is not None:
                        session.delete(ticket)

        finally:
            engine.dispose()
