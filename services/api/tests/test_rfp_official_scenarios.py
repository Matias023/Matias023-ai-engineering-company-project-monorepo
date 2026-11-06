"""Synthetic integration cases based on the official TrackFlow briefing."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

import agent_tracing
import database
import routers.rfp_intake as router

from data.pipelines.rfp_intake.rfp_approval_graph import APPROVERS
from rfp_models import (
    RFPTicket,
    RFPMetadata,
    RFPDepartmentSection,
    RFPFinalDocument,
    utcnow,
)


EMAIL_ENV = {
    "warehouse": "RFP_APPROVER_WAREHOUSE_EMAIL",
    "lastmile": "RFP_APPROVER_LASTMILE_EMAIL",
    "reverse": "RFP_APPROVER_REVERSE_EMAIL",
}


@pytest.mark.parametrize(
    "scenario,country,currency,metrics",
    [
        (
            "ModaViva",
            "Spain",
            "EUR",
            {
                "warehouse": 6000,
                "reverse": 48,
            },
        ),
        (
            "LunaCosmetics",
            "United States",
            "USD",
            {
                "warehouse": 6000,
                "lastmile": 5000,
            },
        ),
    ],
)
def test_official_scenario_commercial_to_final_document(
    scenario,
    country,
    currency,
    metrics,
    monkeypatch,
    tmp_path,
):
    load_dotenv("services/api/.env")

    source = database.get_engine()
    assert source.dialect.name == "postgresql"

    engine = create_engine(source.url.set(database="rfp_test"))

    ticket_id = str(uuid4())
    rfp_id = str(uuid4())
    owner_uuid = str(uuid4())
    departments = list(metrics)

    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT current_database()")
            ).scalar_one() == "rfp_test"

        with Session(engine) as session:
            with session.begin():
                session.add(
                    RFPTicket(
                        ticket_id=ticket_id,
                        rfp_id=rfp_id,
                        owner_uuid=owner_uuid,
                        status="esperando_aprobación",
                        raw_pdf_path=f"test-only/{scenario}.pdf",
                    )
                )
                session.flush()

                session.add(
                    RFPMetadata(
                        rfp_id=rfp_id,
                        ticket_id=ticket_id,
                        client_name=f"SYNTHETIC-{scenario}",
                        client_country=country,
                        services_requested=departments,
                        departments_needed=departments,
                    )
                )

                for department in departments:
                    session.add(
                        RFPDepartmentSection(
                            ticket_id=ticket_id,
                            department_id=department,
                            key_aspects=["Synthetic test aspect"],
                            draft_content=(
                                f"SYNTHETIC {department} PROPOSAL\n"
                                "On-time delivery SLA 98%.\n"
                                "| Monthly volume | Volume discount |\n"
                                "| --- | --- |\n"
                                "| 1000 | 5% |"
                            ),
                            approval_status="pending",
                            evaluation_results={
                                "generation_status": "completed",
                                "review_status": "evaluation_passed",
                                "currency": currency,
                            },
                        )
                    )

        for department in departments:
            monkeypatch.setenv(
                EMAIL_ENV[department],
                f"{department}@test.invalid",
            )

        with (
            patch.object(router, "get_engine", return_value=engine),
            patch.object(
                agent_tracing,
                "TRACE_FILE",
                tmp_path / "rfp-integration-trace.jsonl",
            ),
        ):
            for department, value in metrics.items():
                user = SimpleNamespace(
                    email=f"{department}@test.invalid",
                    is_active=True,
                )

                response = router.confirm_rfp_commercial_metric(
                    ticket_id,
                    department,
                    router.RFPCommercialMetricConfirmation(
                        value=value,
                        source_reference=(
                            f"TEST-ONLY-{scenario}-{department}"
                        ),
                    ),
                    user=user,
                )

                assert response["status"] == "confirmed"

                with Session(engine) as session:
                    with session.begin():
                        section = session.exec(
                            select(RFPDepartmentSection).where(
                                RFPDepartmentSection.ticket_id == ticket_id,
                                RFPDepartmentSection.department_id
                                == department,
                            )
                        ).one()

                        verified = section.evaluation_results[
                            "verified_commercial_metrics"
                        ]

                        assert verified[response["metric"]] == value
                        assert verified["confirmed_by"] == user.email

                        section.approval_status = "approved"
                        section.approver = APPROVERS[department]
                        section.approved_at = utcnow()
                        session.add(section)

            result = router.finalize_rfp_ticket(
                ticket_id,
                user=SimpleNamespace(
                    email="integration@test.invalid",
                    uuid=owner_uuid,
                    is_active=True,
                ),
            )

            assert result["status"] == "terminado"

            # Download must return the persisted final document.
            response = router.download_final_rfp_document(
                ticket_id,
                _user=SimpleNamespace(
                    email="integration@test.invalid",
                    uuid=owner_uuid,
                    is_active=True,
                ),
            )

            assert response.status_code == 200
            assert response.media_type.startswith("text/markdown")
            assert response.headers["content-disposition"].startswith(
                "attachment;"
            )
            assert f"rfp-{ticket_id}.md" in (
                response.headers["content-disposition"]
            )
            assert b"SYNTHETIC" in response.body

        with Session(engine) as session:
            ticket = session.get(RFPTicket, ticket_id)
            document = session.get(RFPFinalDocument, ticket_id)

            assert ticket is not None
            assert ticket.status == "terminado"
            assert document is not None
            assert "SYNTHETIC" in document.content

        trace_path = tmp_path / "rfp-integration-trace.jsonl"

        assert trace_path.is_file()
        assert "rfp_arbitration_evaluated" in trace_path.read_text()

        print(f"{scenario}: INTEGRATION PASS")

    finally:
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT current_database()")
                ).scalar_one() == "rfp_test"

            with Session(engine) as session:
                with session.begin():
                    document = session.get(
                        RFPFinalDocument,
                        ticket_id,
                    )

                    if document is not None:
                        session.delete(document)

                    session.flush()

                    for model in (
                        RFPDepartmentSection,
                        RFPMetadata,
                    ):
                        rows = session.exec(
                            select(model).where(
                                model.ticket_id == ticket_id
                            )
                        ).all()

                        for row in rows:
                            session.delete(row)

                        session.flush()

                    ticket = session.get(RFPTicket, ticket_id)

                    if ticket is not None:
                        session.delete(ticket)
        finally:
            engine.dispose()
