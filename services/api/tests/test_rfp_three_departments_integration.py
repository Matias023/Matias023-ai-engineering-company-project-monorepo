"""Three-department RFP generation against isolated PostgreSQL."""

from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from database import get_engine
from rfp_models import RFPTicket, RFPMetadata, RFPDepartmentSection


DEPARTMENTS = ("warehouse", "lastmile", "reverse")


def test_three_department_generation(monkeypatch):
    engine = get_engine()

    with engine.connect() as connection:
        database_name = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()

    assert engine.dialect.name == "postgresql"
    assert database_name == "rfp_test", "SAFE ABORT: unexpected database"

    ticket_id = str(uuid4())
    rfp_id = str(uuid4())

    def fake_review(department_id, aspects, metadata, **kwargs):
        return {
            "department_id": department_id,
            "currency": "EUR",
            "review_status": "needs_human_review",
            "iterations": 1,
            "draft_content": f"SYNTHETIC {department_id} PROPOSAL",
            "missing_information": ["Human confirmation required"],
            "evaluation_results": {"evaluators": []},
        }

    try:
        with Session(engine) as session:
            with session.begin():
                session.add(
                    RFPTicket(
                        ticket_id=ticket_id,
                        rfp_id=rfp_id,
                        status="analisis_completo",
                        raw_pdf_path="test-only/three-departments.pdf",
                    )
                )
                session.flush()

                session.add(
                    RFPMetadata(
                        rfp_id=rfp_id,
                        ticket_id=ticket_id,
                        client_name="SYNTHETIC-THREE-DEPARTMENTS",
                        client_country="Spain",
                        services_requested=list(DEPARTMENTS),
                        departments_needed=list(DEPARTMENTS),
                    )
                )

                for department_id in DEPARTMENTS:
                    session.add(
                        RFPDepartmentSection(
                            ticket_id=ticket_id,
                            department_id=department_id,
                            key_aspects=[f"Test aspect {department_id}"],
                        )
                    )

        import data.pipelines.rfp_intake.rfp_response_cycle as cycle
        from tasks.rfp_intake import generate_rfp_response

        monkeypatch.setattr(cycle, "run_department_review", fake_review)

        expected_states = (
            "generando_borrador",
            "generando_borrador",
            "needs_human_review",
        )

        for department_id, expected_state in zip(
            DEPARTMENTS, expected_states
        ):
            result = generate_rfp_response.run(ticket_id, department_id)

            assert result["department_id"] == department_id
            assert result["review_status"] == "needs_human_review"

            with Session(engine) as session:
                ticket = session.get(RFPTicket, ticket_id)
                assert ticket.status == expected_state

                sections = session.exec(
                    select(RFPDepartmentSection).where(
                        RFPDepartmentSection.ticket_id == ticket_id
                    )
                ).all()

                completed = [
                    section
                    for section in sections
                    if section.evaluation_results.get("generation_status")
                    == "completed"
                ]

                assert len(completed) == DEPARTMENTS.index(department_id) + 1

            print(
                f"{department_id}: {expected_state} PASS",
                flush=True,
            )

        print("THREE_DEPARTMENT_INTEGRATION: PASS", flush=True)

    finally:
        try:
            with Session(engine) as session:
                with session.begin():
                    for model in (RFPDepartmentSection, RFPMetadata):
                        rows = session.exec(
                            select(model).where(
                                model.ticket_id == ticket_id
                            )
                        ).all()
                        for row in rows:
                            session.delete(row)

                    ticket = session.get(RFPTicket, ticket_id)
                    if ticket is not None:
                        session.delete(ticket)
        finally:
            engine.dispose()
