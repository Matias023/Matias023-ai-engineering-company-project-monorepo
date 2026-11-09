"""Gemini-backed classification for TrackFlow RFP documents."""

import os

from google import genai
from google.genai import types

from pydantic import ConfigDict
from .classification_schema import RFPClassification


class GeminiResponseSchema(RFPClassification):
    # Gemini does not accept additionalProperties in response_schema.
    # Strict validation remains in RFPClassification.
    model_config = ConfigDict(extra="ignore")



SYSTEM_INSTRUCTION = """
You classify logistics documents received by TrackFlow.

An RFP is a client asking for logistics services or a proposal.
Accept both formal RFPs and informal client requests.
Reject vendor pitches, advertising and unrelated documents.

Available departments:
- warehouse: storage, fulfillment, inventory, picking and packing.
- lastmile: final delivery, carriers and distribution to recipients.
- reverse: returns, reverse logistics and returned goods processing.

Extract only facts explicitly supported by the document.
Never invent names, countries, volumes, budgets or deadlines.
Use null when a scalar value is absent.
Do not activate departments for services not requested.

Evidence must contain short, exact excerpts copied verbatim
from the document. Include evidence for:
- Why this document is or is not an RFP.
- Every requested logistics department.
- Every extracted non-null field: client_name, client_country,
  monthly_volume, deadline and budget_range.
- Every service included in services_requested.

When extracting a client name, include an excerpt containing the
exact client name used in client_name.
When extracting monthly_volume, include the original numeric value
as written in the document.

Never invent supporting evidence.
If a scalar field cannot be supported by an exact excerpt,
return null for that field.
Do not activate a department based only on a negated service.

For non-RFP documents, use document_type="not_rfp",
is_rfp=false and departments_needed=[].

Return data matching the required schema.
"""



def _normalize_evidence(text: str) -> str:
    """Ignore Markdown styling and whitespace, preserving literal content."""
    import re
    import unicodedata

    text = re.sub(r"\|+", " ", text)
    text = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())



def _evidence_in_document(markdown: str, excerpt: str) -> bool:
    """Match textual evidence without silently dropping immediate negations."""
    import re

    document = _normalize_evidence(markdown)
    quote = _normalize_evidence(excerpt)

    if not quote:
        return False

    pattern = re.compile(
        r"(?<!\w)" + re.escape(quote) + r"(?!\w)"
    )

    for match in pattern.finditer(document):
        prefix = document[:match.start()]

        if re.search(
            r"(?:^|[\s.!?;:])(?:no|not|sin|without)\s+$",
            prefix,
        ):
            continue

        return True

    return False



def _country_supported_by_evidence(
    country: str,
    evidence: list[str],
) -> bool:
    """Validate country aliases against existing textual evidence."""
    import re
    import unicodedata

    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold())
        value = "".join(
            char for char in value
            if not unicodedata.combining(char)
        )
        return " ".join(re.findall(r"[a-z0-9]+", value))

    aliases = {
        "spain": {
            "spain", "espana", "es",
            "espanola", "espanol", "spanish",
        },
        "united_states": {
            "united states", "estados unidos",
            "usa", "us", "u s", "u s a",
            "ee uu", "estadounidense",
        },
    }

    normalized_country = normalize(country)

    selected = {normalized_country}

    for group in aliases.values():
        if normalized_country in group:
            selected = group
            break

    for excerpt in evidence:
        normalized_excerpt = " " + normalize(excerpt) + " "

        for alias in selected:
            normalized_alias = normalize(alias)

            if (
                normalized_alias
                and f" {normalized_alias} " in normalized_excerpt
            ):
                return True

    return False


def classify_rfp(
    markdown: str,
    *,
    client=None,
    model: str | None = None,
) -> RFPClassification:
    """Classify document content using structured Gemini output."""

    if not markdown.strip():
        raise ValueError("Cannot classify an empty document")

    selected_model = model or os.getenv("GEMINI_MODEL")

    if not selected_model:
        raise ValueError("GEMINI_MODEL must be configured")

    if client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=selected_model,
        contents=markdown,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=GeminiResponseSchema,
        ),
    )

    if not response.text:
        raise ValueError("Gemini returned no classification")

    result = RFPClassification.model_validate_json(response.text)

    if not result.evidence:
        raise ValueError("Classification has no textual evidence")

    for excerpt in result.evidence:
        if not _evidence_in_document(markdown, excerpt):
            raise ValueError("Classification evidence not found in document")

    if (
        result.client_country is not None
        and not _country_supported_by_evidence(
            result.client_country,
            result.evidence,
        )
    ):
        raise ValueError(
            "Unsupported extracted metadata: client_country"
        )

    for field in (
        "client_name",
        "deadline",
        "budget_range",
    ):
        value = getattr(result, field)
        if value is None:
            continue

        normalized_value = " ".join(value.casefold().split())

        supported = any(
            normalized_value in " ".join(quote.casefold().split())
            for quote in result.evidence
        )

        if not supported:
            raise ValueError(
                f"Unsupported extracted metadata: {field}"
            )

    import re
    import unicodedata

    def normalize(text):
        text = unicodedata.normalize("NFKD", text.casefold())
        return "".join(
            char for char in text
            if not unicodedata.combining(char)
        )

    evidence_text = normalize(" ".join(result.evidence))

    if result.monthly_volume is not None:
        numbers = re.findall(
            r"(?<!\w)\d[\d., ]*(?!\w)",
            evidence_text,
        )
        values = {
            int(re.sub(r"\D", "", number))
            for number in numbers
            if re.sub(r"\D", "", number)
        }
        if result.monthly_volume not in values:
            raise ValueError(
                "Unsupported extracted metadata: monthly_volume"
            )

    service_terms = {
        "warehouse": (
            "almacen", "bodega", "inventario",
            "picking", "packing", "fulfillment",
            "storage", "warehous",
        ),
        "lastmile": (
            "ultima milla", "last mile",
            "entrega", "distribucion", "delivery",
        ),
        "reverse": (
            "devolucion", "devoluciones",
            "logistica inversa", "retorno",
            "returns", "reverse logistics",
        ),
    }

    for department in result.departments_needed:
        for term in service_terms[department]:
            pattern = (
                r"\b(?:no\s+(?:necesitamos|requerimos|"
                r"solicitamos|queremos|incluimos)|sin)"
                r"\s+(?:\w+\s+){0,4}"
                + re.escape(term)
                + r"\b"
            )
            if re.search(pattern, evidence_text):
                raise ValueError(
                    f"Negated service requires review: {department}"
                )
        if not any(
            term in evidence_text
            for term in service_terms[department]
        ):
            raise ValueError(
                f"Unsupported department: {department}"
            )

    return result
