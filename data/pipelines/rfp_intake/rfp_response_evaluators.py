"""Structured TrackFlow proposal evaluators — Hito 9, Part 2."""

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EvaluatorType = Literal["readability", "relevance", "compliance"]

EVALUATOR_INSTRUCTIONS = {
    "readability": (
        "Evaluate whether the proposal is understandable, professionally "
        "written, logically structured and sufficiently clear for a client. "
        "Identify specific unclear passages."
    ),
    "relevance": (
        "Evaluate whether the proposal addresses the supplied department "
        "key_aspects and requested services without unrelated content. "
        "Identify omitted or unsupported requirements."
    ),
    "compliance": (
        "Evaluate TrackFlow's commercial rules strictly: "
        "US operations require USD and Spain operations require EUR; "
        "the proposal must address the on-time delivery SLA percentage; "
        "it must include a volume-discount table; "
        "returns processing must never be promised below 48 hours; any specific numeric returns-processing timeframe, including 48 hours or greater, requires verified documentary evidence; unsupported time commitments are violations; explicit human feedback requiring removal of unsupported timeframes must be enforced; "
        "negotiated carrier-specific rates must remain confidential. "
        "Missing or unconfirmed commitments must not be marked compliant. "
        "Identify every violated or unverified requirement."
    ),
}


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator: EvaluatorType
    passed: bool
    score: float = Field(ge=0, le=1)
    feedback: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)



class GeminiEvaluationResponse(EvaluationResult):
    """Gemini-compatible schema; strict validation follows."""
    model_config = ConfigDict(extra="ignore")


def evaluate_proposal(
    evaluator: EvaluatorType,
    department_id: str,
    draft_content: str,
    key_aspects: list[str],
    metadata: dict[str, Any],
    *,
    client=None,
    model: str | None = None,
) -> EvaluationResult:
    """Evaluate one draft; never assume missing information is compliant."""

    if evaluator not in EVALUATOR_INSTRUCTIONS:
        raise ValueError("Unknown evaluator")

    if department_id not in {"warehouse", "lastmile", "reverse"}:
        raise ValueError("Unknown department")

    if not isinstance(draft_content, str) or not draft_content.strip():
        raise ValueError("Draft content is required")

    if not isinstance(key_aspects, list) or not key_aspects:
        raise ValueError("Department key_aspects are required")

    if not isinstance(metadata, dict):
        raise ValueError("RFP metadata must be a dictionary")

    selected_model = model or os.getenv("GEMINI_MODEL")
    if not selected_model:
        raise ValueError("GEMINI_MODEL must be configured")

    if client is None:
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        client = genai.Client(api_key=api_key)

    from google.genai import types

    instructions = (
        EVALUATOR_INSTRUCTIONS[evaluator] + "\n"
        "Return a strict JSON object with evaluator, passed, score, "
        "feedback and violations. "
        "score must be between 0 and 1. "
        "Do not fabricate facts, prices, evidence or approvals. "
        "If passed=false, give actionable feedback. "
        "For compliance, report each violated or missing rule. "
        f"The evaluator field must equal {evaluator!r}."
    )

    payload = {
        "evaluator": evaluator,
        "department_id": department_id,
        "draft_content": draft_content,
        "key_aspects": key_aspects,
        "metadata": metadata,
    }

    from .gemini_rate_limit import wait_for_gemini_slot
    wait_for_gemini_slot()

    response = client.models.generate_content(
        model=selected_model,
        contents=json.dumps(payload, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=instructions,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema={
                "type": "object",
                "properties": {
                    "evaluator": {
                        "type": "string",
                        "enum": [
                            "readability",
                            "relevance",
                            "compliance",
                        ],
                    },
                    "passed": {"type": "boolean"},
                    "score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "feedback": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "violations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "evaluator",
                    "passed",
                    "score",
                    "feedback",
                    "violations",
                ],
            },
            http_options=types.HttpOptions(timeout=120_000),
        ),
    )

    if not response.text:
        raise ValueError("Evaluator returned empty response")

    payload = json.loads(response.text)

    if not isinstance(payload, dict):
        raise ValueError("Evaluator must return a JSON object")

    for field in ("feedback", "violations"):
        value = payload.get(field)

        if isinstance(value, str):
            payload[field] = [value] if value.strip() else []
        elif value is None:
            payload[field] = []

    result = EvaluationResult.model_validate(payload)

    if result.evaluator != evaluator:
        raise ValueError("Evaluator identity mismatch")

    if not result.passed and not (result.feedback or result.violations):
        raise ValueError("Failed evaluation requires feedback")

    if result.passed and result.violations:
        # Fail closed: a contradictory evaluation cannot approve.
        result = result.model_copy(
            update={
                "passed": False,
                "feedback": [
                    *result.feedback,
                    "Evaluator reported violations while marking "
                    "the proposal as passed; approval denied.",
                ],
            }
        )

    return result
