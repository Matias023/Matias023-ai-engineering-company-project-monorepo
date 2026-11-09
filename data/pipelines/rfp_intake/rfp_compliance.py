"""Deterministic checks for explicit TrackFlow commercial violations."""

import re



def check_client_identity(draft: str, client_name: str | None) -> list[str]:
    """Detect explicit client-name mismatches without guessing identity."""
    if not client_name or not isinstance(client_name, str):
        return []

    if not isinstance(draft, str) or not draft.strip():
        return ["Empty proposal"]

    import unicodedata

    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFC", value)
        return " ".join(value.casefold().split())

    expected = normalize(client_name)

    if expected in normalize(draft):
        return []

    # Absence alone is not proof of an incorrect identity.
    # Only flag an explicit client declaration inconsistent with metadata.
    pattern = re.compile(
        r"(?im)^(?:cliente|client|destinatario|prepared for)"
        r"\s*[:：]\s*(.+)$"
    )

    for match in pattern.finditer(draft):
        declared = normalize(match.group(1).strip())

        if declared and expected not in declared:
            return ["Client identity mismatch"]

    return []


def check_commercial_rules(
    draft: str,
    country: str,
) -> list[str]:
    """Return detected violations; empty is not proof of full compliance."""

    if not isinstance(draft, str) or not draft.strip():
        return ["Empty proposal"]

    issues = []
    country_key = country.strip().casefold() if isinstance(country, str) else ""

    if country_key in {"usa", "us", "united states"}:
        expected = "USD"
        forbidden = "EUR"
    elif country_key in {"spain", "españa"}:
        expected = "EUR"
        forbidden = "USD"
    else:
        return ["Unsupported or unconfirmed client country"]

    if re.search(rf"\b{forbidden}\b", draft, re.IGNORECASE):
        issues.append(
            f"Currency mismatch: {forbidden} appears in a {expected} proposal"
        )

    # Only flag explicit promises; avoid mistaking references to the
    # client's original requirements for TrackFlow commitments.
    promise = re.compile(
        r"(?:procesaremos|procesamiento garantizado|"
        r"garantizamos procesar|we guarantee processing|"
        r"we will process)"
        r"[^.\n]{0,100}?"
        r"(\d{1,3})\s*(?:horas|hours|hrs)\b",
        re.IGNORECASE,
    )

    for match in promise.finditer(draft):
        if int(match.group(1)) < 48:
            issues.append(
                "Returns processing commitment below 48 hours"
            )

    # Missing commitments cannot be approved as confirmed compliance.
    sla_pattern = re.compile(
        r"(?:sla|on.time delivery|entregas? a tiempo)"
        r"[^.\n]{0,100}?\b\d{1,3}(?:[.,]\d+)?\s*%",
        re.IGNORECASE,
    )
    if not sla_pattern.search(draft):
        issues.append("On-time delivery SLA percentage missing")

    # A keyword alone is not a volume-discount table.
    # Require a Markdown header, separator and numeric discount row.
    lines = draft.splitlines()
    has_discount_table = False

    for index in range(len(lines) - 2):
        header = lines[index].strip()

        if not (header.startswith("|") and header.endswith("|")):
            continue

        columns = [
            cell.strip().casefold()
            for cell in header.strip("|").split("|")
        ]

        has_volume = any(
            "volume" in cell or "volumen" in cell
            for cell in columns
        )
        has_discount = any(
            "discount" in cell or "descuento" in cell
            for cell in columns
        )

        if not (has_volume and has_discount):
            continue

        separator = lines[index + 1].strip()
        row = lines[index + 2].strip()

        if not (
            separator.startswith("|")
            and re.fullmatch(r"[\s|:-]+", separator)
            and "-" in separator
            and row.startswith("|")
            and row.endswith("|")
            and re.search(r"\d", row)
            and re.search(r"\d+(?:[.,]\d+)?\s*%", row)
        ):
            continue

        has_discount_table = True
        break

    if not has_discount_table:
        issues.append("Volume discount table missing or unconfirmed")

    confidential_rates = re.compile(
        r"(?:tarifa negociada|negotiated carrier rate)"
        r"[^.\n]{0,100}?(?:\d+[.,]\d*|USD|EUR|\$|€)",
        re.IGNORECASE,
    )
    if confidential_rates.search(draft):
        issues.append("Potential disclosure of negotiated carrier rates")

    return issues
