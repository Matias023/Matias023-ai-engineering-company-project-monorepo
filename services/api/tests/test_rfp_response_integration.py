"""Integration test for RFP response generation against isolated PostgreSQL."""

import os
import sys
import uuid

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/services/api")

from sqlalchemy import text
from sqlmodel import SQLModel, Session, select
from unittest.mock import patch
from database import get_engine as original_get_engine


def get_engine():
    import subprocess
    from sqlalchemy import create_engine
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

    return create_engine(url, connect_args={"connect_timeout": 5})

from rfp_models import RFPTicket, RFPMetadata, RFPDepartmentSection
import data.pipelines.rfp_intake.rfp_response_cycle as cycle


def main():
    engine = get_engine()

    with engine.connect() as connection:
        database_name = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()

    assert engine.dialect.name == "postgresql", engine.dialect.name
    assert database_name == "rfp_regression_test", (
        f"Refusing to test against unexpected database: {database_name}"
    )
    print("DATABASE ISOLATION: PASS", flush=True)

    SQLModel.metadata.create_all(engine)

    ticket_id = str(uuid.uuid4())
    rfp_id = str(uuid.uuid4())

    def fake_review(department_id, aspects, metadata, **kwargs):
        return {
            "department_id": department_id,
            "currency": "EUR",
            "review_status": "needs_human_review",
            "iterations": 1,
            "draft_content": "Temporary integration-test proposal",
            "missing_information": ["Pricing requires confirmation"],
            "evaluation_results": {
                "evaluators": [
                    {
                        "evaluator": "compliance",
                        "passed": False,
                        "score": 0,
                        "feedback": ["Commercial data is unconfirmed"],
                        "violations": ["Missing confirmed commitments"],
                    }
                ]
            },
        }

    try:
        with Session(engine) as session:
            session.add(RFPTicket(
                ticket_id=ticket_id,
                rfp_id=rfp_id,
                status="analisis_completo",
                raw_pdf_path="test-only/temporary.pdf",
            ))
            session.commit()

            session.add(RFPMetadata(
                rfp_id=rfp_id,
                ticket_id=ticket_id,
                client_name="Temporary test client",
                client_country="Spain",
                services_requested=["warehouse"],
                departments_needed=["warehouse"],
            ))
            session.commit()

            session.add(RFPDepartmentSection(
                ticket_id=ticket_id,
                department_id="warehouse",
                key_aspects=["Storage proposal"],
            ))
            session.commit()

        print("TEST FIXTURES: PASS", flush=True)

        # Patch the source module before the task imports the function.
        cycle.run_department_review = fake_review

        from tasks.rfp_intake import generate_rfp_response

        with patch("database.get_engine", return_value=engine):
            result = generate_rfp_response.run(ticket_id, "warehouse")

        assert result["ticket_id"] == ticket_id
        assert result["department_id"] == "warehouse"
        assert result["review_status"] == "needs_human_review"
        assert result["iterations"] == 1
        print("TASK RESULT CONTRACT: PASS", flush=True)

        with Session(engine) as session:
            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id,
                    RFPDepartmentSection.department_id == "warehouse",
                )
            ).one()

            ticket = session.get(RFPTicket, ticket_id)

            assert section.draft_content == (
                "Temporary integration-test proposal"
            )
            assert (
                section.evaluation_results["generation_status"]
                == "completed"
            )
            assert (
                section.evaluation_results["review_status"]
                == "needs_human_review"
            )
            assert section.evaluation_results["currency"] == "EUR"
            assert ticket.status == "needs_human_review"

        print("DRAFT PERSISTENCE: PASS", flush=True)
        print("EVALUATION PERSISTENCE: PASS", flush=True)
        print("HUMAN REVIEW STATUS: PASS", flush=True)

        try:
            with patch("database.get_engine", return_value=engine):
                generate_rfp_response.run(ticket_id, "warehouse")
        except ValueError:
            print("DUPLICATE PREVENTION: PASS", flush=True)
        else:
            raise AssertionError("Duplicate generation was accepted")

        print("F5 INTEGRATION: PASS", flush=True)

    finally:
        with Session(engine) as session:
            for model in (RFPDepartmentSection, RFPMetadata):
                rows = session.exec(
                    select(model).where(model.ticket_id == ticket_id)
                ).all()
                for item in rows:
                    session.delete(item)

            ticket = session.get(RFPTicket, ticket_id)
            if ticket is not None:
                session.delete(ticket)

            session.commit()

        engine.dispose()
        print("TEST CLEANUP: PASS", flush=True)


def test_generation_failure():
    """Verify that generator failures persist a recoverable failed state."""
    engine = get_engine()

    with engine.connect() as connection:
        db_name = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()

    assert engine.dialect.name == "postgresql"
    assert db_name == "rfp_regression_test", db_name

    ticket_id = str(uuid.uuid4())
    rfp_id = str(uuid.uuid4())

    def failing_review(*args, **kwargs):
        raise RuntimeError("Simulated Gemini failure")

    try:
        with Session(engine) as session:
            session.add(RFPTicket(
                ticket_id=ticket_id,
                rfp_id=rfp_id,
                status="analisis_completo",
                raw_pdf_path="test-only/failure.pdf",
            ))
            session.commit()

            session.add(RFPMetadata(
                rfp_id=rfp_id,
                ticket_id=ticket_id,
                client_name="Failure test client",
                client_country="Spain",
                services_requested=["warehouse"],
                departments_needed=["warehouse"],
            ))
            session.commit()

            session.add(RFPDepartmentSection(
                ticket_id=ticket_id,
                department_id="warehouse",
                key_aspects=["Storage proposal"],
            ))
            session.commit()

        original_review = cycle.run_department_review
        cycle.run_department_review = failing_review

        try:
            from tasks.rfp_intake import generate_rfp_response

            try:
                with patch("database.get_engine", return_value=engine):
                    generate_rfp_response.run(ticket_id, "warehouse")
            except RuntimeError as exc:
                assert str(exc) == "Simulated Gemini failure"
                print("GENERATOR FAILURE PROPAGATION: PASS", flush=True)
            else:
                raise AssertionError("Generator failure was swallowed")
        finally:
            cycle.run_department_review = original_review

        with Session(engine) as session:
            section = session.exec(
                select(RFPDepartmentSection).where(
                    RFPDepartmentSection.ticket_id == ticket_id
                )
            ).one()

            assert section.evaluation_results["generation_status"] == "failed"
            assert section.evaluation_results["generation_error_type"] == "RuntimeError"
            assert section.draft_content is None

        print("FAILURE STATE PERSISTENCE: PASS", flush=True)
        print("NO PARTIAL DRAFT: PASS", flush=True)
        print("F6 FAILURE TEST: PASS", flush=True)

    finally:
        with Session(engine) as session:
            for model in (RFPDepartmentSection, RFPMetadata):
                rows = session.exec(
                    select(model).where(model.ticket_id == ticket_id)
                ).all()
                for row in rows:
                    session.delete(row)

            ticket = session.get(RFPTicket, ticket_id)
            if ticket is not None:
                session.delete(ticket)

            session.commit()

        engine.dispose()
        print("FAILURE TEST CLEANUP: PASS", flush=True)


if __name__ == "__main__":
    main()
    test_generation_failure()
