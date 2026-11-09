"""RFP commercial compliance regression tests."""

import pytest

from data.pipelines.rfp_intake.rfp_compliance import (
    check_commercial_rules,
)
from data.pipelines.rfp_intake.rfp_final_document import (
    FinalDocumentBlocked,
    generate_final_document,
)


def test_discount_keyword_without_table_is_rejected():
    draft = (
        "On-time delivery SLA 98%. "
        "We provide a volume discount table."
    )

    issues = check_commercial_rules(draft, "Spain")

    assert "Volume discount table missing or unconfirmed" in issues


@pytest.mark.parametrize(
    "header",
    [
        "| Monthly volume | Volume discount |",
        "| Volumen mensual | Descuento |",
    ],
)
def test_numeric_volume_discount_table_is_recognized(header):
    draft = (
        "On-time delivery SLA 98%.\n"
        f"{header}\n"
        "| --- | --- |\n"
        "| 1000 | 5% |"
    )

    issues = check_commercial_rules(draft, "Spain")

    assert "Volume discount table missing or unconfirmed" not in issues


def test_confidential_rate_blocks_final_document():
    sections = {
        "warehouse": {
            "approval_status": "approved",
            "draft_content": (
                "Approved proposal. "
                "Negotiated carrier rate EUR 12."
            ),
            "currency": "EUR",
            "approver": "SYNTHETIC APPROVER",
            "approved_at": "2026-01-01T00:00:00Z",
        },
    }

    with pytest.raises(
        FinalDocumentBlocked,
        match="Confidential carrier rate",
    ):
        generate_final_document(
            "SYNTHETIC-TICKET",
            "Spain",
            ["warehouse"],
            sections,
        )


def test_ordinary_approved_document_remains_eligible():
    sections = {
        "warehouse": {
            "approval_status": "approved",
            "draft_content": "Approved synthetic proposal.",
            "currency": "EUR",
            "approver": "SYNTHETIC APPROVER",
            "approved_at": "2026-01-01T00:00:00Z",
        },
    }

    document = generate_final_document(
        "SYNTHETIC-TICKET",
        "Spain",
        ["warehouse"],
        sections,
    )

    assert document["currency"] == "EUR"
    assert len(document["sections"]) == 1


def test_client_identity_mismatch_blocks_automatic_review():
    from data.pipelines.rfp_intake.rfp_response_cycle import run_department_review
    from data.pipelines.rfp_intake.rfp_response_evaluators import EvaluationResult

    draft = """Cliente: Logistical NovaRetail S.L.
Propuesta comercial de almacenamiento.
SLA de entregas a tiempo: 98% solicitado, pendiente de confirmación.

| Volumen mensual | Descuento |
| --- | --- |
| 1000 | 5% |
"""

    def fake_generator(department_id, key_aspects, metadata, *, feedback=None):
        return {
            "draft_content": draft,
            "currency": "EUR",
            "missing_information": [],
        }

    def fake_evaluator(name, department_id, draft_content, key_aspects, metadata):
        return EvaluationResult(
            evaluator=name,
            passed=True,
            score=1.0,
            feedback=[],
            violations=[],
        )

    result = run_department_review(
        "warehouse",
        ["Almacenamiento"],
        {
            "client_country": "España",
            "client_name": "Logística NovaRetail S.L.",
        },
        max_iterations=1,
        generator=fake_generator,
        evaluator=fake_evaluator,
    )

    compliance = next(
        item for item in result["evaluation_results"]["evaluators"]
        if item["evaluator"] == "compliance"
    )

    assert result["review_status"] == "needs_human_review"
    assert result["evaluation_results"]["passed"] is False
    assert compliance["passed"] is False
    assert "Client identity mismatch" in compliance["violations"]
