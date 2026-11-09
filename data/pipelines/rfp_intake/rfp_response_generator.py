"""Department-specific commercial proposal generation for TrackFlow."""

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .rfp_orchestrator import DEPARTMENT_OWNERS


DEPARTMENT_PROPOSAL_SCOPES = {
    "warehouse": (
        "Write the warehousing proposal section: storage, "
        "receiving, inventory management, fulfillment, "
        "onboarding and any confirmed commercial conditions."
    ),
    "lastmile": (
        "Write the last-mile logistics proposal section: "
        "delivery coverage, final customer pricing, "
        "delivery SLA and carrier-management services."
    ),
    "reverse": (
        "Write the reverse logistics proposal section: "
        "returns receiving, inspection, processing times "
        "and confirmed commercial conditions."
    ),
}


class GeneratedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_content: str = Field(min_length=1)
    missing_information: list[str] = Field(default_factory=list)



class GeminiGeneratedProposalResponse(GeneratedProposal):
    """Gemini-compatible schema; strict validation follows."""
    model_config = ConfigDict(extra="ignore")


def generate_department_proposal(
    department_id: str,
    key_aspects: list[str],
    metadata: dict[str, Any],
    *,
    feedback: str | None = None,
    client=None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate a proposal using the validated Part 1 handoff."""

    if department_id not in DEPARTMENT_PROPOSAL_SCOPES:
        raise ValueError(f"Unknown department: {department_id}")

    if not isinstance(key_aspects, list) or not key_aspects:
        raise ValueError("Department key_aspects are required")

    if not all(
        isinstance(item, str) and item.strip()
        for item in key_aspects
    ):
        raise ValueError("Invalid key_aspects")

    country = metadata.get("client_country")

    if country in ("USA", "US", "United States"):
        currency = "USD"
    elif country in ("Spain", "España"):
        currency = "EUR"
    else:
        raise ValueError("Unsupported client country")

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
    import json

    instructions = (
        DEPARTMENT_PROPOSAL_SCOPES[department_id] + "\n"
        f"Required proposal currency: {currency}.\n"
        "Write the proposal in Spanish when the client country is Spain. "
        "Preserve the exact client_name from metadata, including accents, "
        "punctuation and legal suffixes. Never translate or rename it. "
        "Keep the proposal focused on the assigned department. "
        "Do not present requested service levels, geographical coverage "
        "or operational capabilities as confirmed provider commitments "
        "unless supported by verified evidence. "
        "Use clear, professional language without unrelated characters. "
        "Use only facts supported by the supplied metadata "
        "and department requirements. "
        "Do not invent prices, costs, discounts, volumes, "
        "capacity, SLAs, timelines or commercial commitments. "
        "Every proposal must address on-time delivery SLA "
        "and a volume discount table, indicating explicitly "
        "when values are pending confirmation. "
        "Never promise returns processing in less than 48 hours. Never promise any specific returns-processing timeframe, including 48 hours or greater, unless that exact timeframe is supported by verified documentary evidence. Otherwise state that processing times and SLAs are pending confirmation. Explicit human feedback requesting removal of an unsupported timeframe must be respected. "
        "Never reveal negotiated carrier rates. "
        "Return JSON with draft_content and missing_information. "
        "If information is missing, describe it as unconfirmed."
    )

    payload = {
        "department_id": department_id,
        "department_owner": DEPARTMENT_OWNERS[department_id],
        "key_aspects": key_aspects,
        "metadata": metadata,
        "currency": currency,
        "feedback_from_previous_evaluation": feedback,
    }

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
                    "draft_content": {"type": "string"},
                    "missing_information": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "draft_content",
                    "missing_information",
                ],
            },
            http_options=types.HttpOptions(timeout=120_000),
        ),
    )

    if not response.text:
        raise ValueError("Generator returned empty response")

    import json

    payload = json.loads(response.text)

    if not isinstance(payload, dict):
        raise ValueError("Generator must return a JSON object")

    draft = payload.get("draft_content")

    if isinstance(draft, dict):
        payload["draft_content"] = "\n".join(
            f"{key}: {value}"
            for key, value in draft.items()
        )
    elif isinstance(draft, list):
        payload["draft_content"] = "\n".join(
            str(item) for item in draft
        )

    generated = GeneratedProposal.model_validate(payload)

    return {
        "department_id": department_id,
        "currency": currency,
        **generated.model_dump(),
    }
