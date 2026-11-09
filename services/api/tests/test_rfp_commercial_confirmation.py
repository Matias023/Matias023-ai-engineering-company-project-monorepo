"""Authorized human confirmation of RFP commercial metrics."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

import database
import routers.rfp_intake as router

from rfp_models import RFPTicket, RFPMetadata, RFPDepartmentSection


@pytest.mark.parametrize(
    "department,value,metric",
    [
        ("warehouse", 2000, "monthly_capacity"),
        ("lastmile", 1500, "monthly_volume"),
        ("reverse", 48, "returns_turnaround_hours"),
    ],
)
def test_commercial_confirmation_authorization_and_persistence(
    monkeypatch, department, value, metric
):
    import subprocess
    from sqlalchemy.engine import URL

    container = "trackflow-rfp-pg-demo"

    result = subprocess.run(
        [
            "docker", "exec", container, "sh", "-c",
            'printf "%s\\n%s\\n" "$POSTGRES_USER" "$POSTGRES_PASSWORD"',
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    credentials = result.stdout.splitlines()

    if len(credentials) != 2 or not all(credentials):
        raise RuntimeError("SAFE ABORT: invalid local PostgreSQL credentials")

    engine = create_engine(
        URL.create(
            "postgresql+psycopg2",
            username=credentials[0],
            password=credentials[1],
            host="127.0.0.1",
            port=55433,
            database="rfp_regression_test",
        )
    )
    ticket_id = str(uuid4())
    rfp_id = str(uuid4())

    email = "authorized-approver@example.invalid"
    env_name = {
        "warehouse": "RFP_APPROVER_WAREHOUSE_EMAIL",
        "lastmile": "RFP_APPROVER_LASTMILE_EMAIL",
        "reverse": "RFP_APPROVER_REVERSE_EMAIL",
    }[department]

    monkeypatch.setenv(env_name, email)

    user = SimpleNamespace(email=email, is_active=True)
    stranger = SimpleNamespace(
        email="stranger@example.invalid",
        is_active=True,
    )

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
                    raw_pdf_path="test-only/confirmation.pdf",
                ))
                session.flush()

                session.add(RFPMetadata(
                    rfp_id=rfp_id,
                    ticket_id=ticket_id,
                    client_name="SYNTHETIC CLIENT",
                    client_country="Spain",
                    services_requested=[department],
                    departments_needed=[department],
                ))

                session.add(RFPDepartmentSection(
                    ticket_id=ticket_id,
                    department_id=department,
                    key_aspects=["Synthetic"],
                    draft_content="SYNTHETIC DRAFT",
                    approval_status="pending",
                    evaluation_results={
                        "generation_status": "completed",
                        "review_status": "evaluation_passed",
                        "currency": "EUR",
                    },
                ))

        payload = router.RFPCommercialMetricConfirmation(
            value=value,
            source_reference="SYNTHETIC-INTERNAL-RECORD",
        )

        with patch.object(router, "get_engine", return_value=engine):
            with pytest.raises(HTTPException) as rejected:
                router.confirm_rfp_commercial_metric(
                    ticket_id, department, payload, user=stranger
                )

            assert rejected.value.status_code == 403

            response = router.confirm_rfp_commercial_metric(
                ticket_id, department, payload, user=user
            )

            assert response["status"] == "confirmed"
            assert response["metric"] == metric

            with Session(engine) as session:
                section = session.exec(
                    select(RFPDepartmentSection).where(
                        RFPDepartmentSection.ticket_id == ticket_id,
                        RFPDepartmentSection.department_id == department,
                    )
                ).one()

                verified = section.evaluation_results[
                    "verified_commercial_metrics"
                ]

                assert verified[metric] == value
                assert verified["confirmed_by"] == email
                assert verified["source_reference"] == (
                    "SYNTHETIC-INTERNAL-RECORD"
                )

                section.approval_status = "approved"
                session.add(section)
                session.commit()

            with pytest.raises(HTTPException) as locked:
                router.confirm_rfp_commercial_metric(
                    ticket_id, department, payload, user=user
                )

            assert locked.value.status_code == 409

    finally:
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT current_database()")
                ).scalar_one() == "rfp_regression_test"

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

                        session.flush()

                    ticket = session.get(RFPTicket, ticket_id)
                    if ticket is not None:
                        session.delete(ticket)
        finally:
            engine.dispose()
