
import json

import pytest

from data.pipelines.rfp_intake.rfp_response_generator import (
    GeneratedProposal,
    GeminiGeneratedProposalResponse,
    generate_department_proposal,
)
from data.pipelines.rfp_intake.rfp_response_evaluators import (
    EvaluationResult,
    GeminiEvaluationResponse,
    evaluate_proposal,
)


class FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.models = self
        self.config = None

    def generate_content(self, *, model, contents, config):
        self.config = config
        return FakeResponse(self.payload)


def test_generator_requests_structured_object():
    client = FakeClient({
        "draft_content": "Storage services pending confirmation.",
        "missing_information": ["Confirmed monthly capacity"],
    })

    result = generate_department_proposal(
        "warehouse",
        ["Storage"],
        {"client_country": "España"},
        client=client,
        model="test-model",
    )

    assert result["currency"] == "EUR"
    assert result["draft_content"]
    assert client.config.response_schema is None
    schema = client.config.response_json_schema
    assert schema["type"] == "object"
    assert "draft_content" in schema["required"]
    assert client.config.http_options.timeout == 120_000
    assert issubclass(
        GeminiGeneratedProposalResponse,
        GeneratedProposal,
    )



@pytest.mark.parametrize("department", ["warehouse", "lastmile", "reverse"])
def test_generator_enforces_commercial_writing_instructions(department):
    client = FakeClient({
        "draft_content": "Propuesta sintética pendiente de confirmación.",
        "missing_information": [],
    })

    generate_department_proposal(
        department,
        ["Synthetic department requirement"],
        {
            "client_country": "España",
            "client_name": "Logística NovaRetail S.L.",
        },
        client=client,
        model="test-model",
    )

    instructions = client.config.system_instruction

    assert "Write the proposal in Spanish" in instructions
    assert "Preserve the exact client_name" in instructions
    assert "Keep the proposal focused on the assigned department" in instructions
    assert "confirmed provider commitments" in instructions
    assert "verified evidence" in instructions


def test_generator_rejects_array_response():
    client = FakeClient([{"draft_content": "Invalid shape"}])

    with pytest.raises(ValueError, match="JSON object"):
        generate_department_proposal(
            "warehouse",
            ["Storage"],
            {"client_country": "España"},
            client=client,
            model="test-model",
        )


def test_evaluator_requests_structured_object():
    client = FakeClient({
        "evaluator": "readability",
        "passed": True,
        "score": 0.9,
        "feedback": [],
        "violations": [],
    })

    result = evaluate_proposal(
        "readability",
        "warehouse",
        "A clear and structured proposal.",
        ["Storage"],
        {"client_country": "España"},
        client=client,
        model="test-model",
    )

    assert result.passed is True
    assert client.config.response_schema is None
    schema = client.config.response_json_schema
    assert schema["type"] == "object"
    assert "violations" in schema["required"]
    assert client.config.http_options.timeout == 120_000
    assert issubclass(GeminiEvaluationResponse, EvaluationResult)


def test_violations_override_positive_evaluation():
    client = FakeClient({
        "evaluator": "compliance",
        "passed": True,
        "score": 0.9,
        "feedback": [],
        "violations": ["Required SLA not confirmed"],
    })

    result = evaluate_proposal(
        "compliance",
        "warehouse",
        "SLA pending confirmation.",
        ["Storage"],
        {"client_country": "España"},
        client=client,
        model="test-model",
    )

    assert result.passed is False
    assert "Required SLA not confirmed" in result.violations
    assert result.feedback


def test_failed_evaluation_without_feedback_is_rejected():
    client = FakeClient({
        "evaluator": "compliance",
        "passed": False,
        "score": 0.2,
        "feedback": [],
        "violations": [],
    })

    with pytest.raises(
        ValueError,
        match="Failed evaluation requires feedback",
    ):
        evaluate_proposal(
            "compliance",
            "warehouse",
            "Draft awaiting review.",
            ["Storage"],
            {"client_country": "España"},
            client=client,
            model="test-model",
        )
