
from data.pipelines.rfp_intake.rfp_classifier import (
    _normalize_evidence,
    _evidence_in_document,
)


def test_markdown_formatting_does_not_invalidate_evidence():
    document = "**RFP Reference No.:** MV-LOG-2026-03"
    evidence = "RFP Reference No.: MV-LOG-2026-03"

    assert _evidence_in_document(document, evidence)


def test_newlines_and_case_do_not_invalidate_evidence():
    document = (
        "**ModaViva** solicita"
        + chr(10) + chr(10)
        + "gestión de devoluciones"
    )
    evidence = "modaviva solicita gestión de devoluciones"

    assert _evidence_in_document(document, evidence)


def test_invented_evidence_is_rejected():
    document = "ModaViva solicita almacenamiento y devoluciones"
    invented = "ModaViva solicita transporte de última milla"

    assert not _evidence_in_document(document, invented)


def test_negated_service_is_not_treated_as_affirmative_evidence():
    document = "No se requiere gestión de transportistas"
    affirmative = "Se requiere gestión de transportistas"

    assert not _evidence_in_document(document, affirmative)


def test_complete_negative_evidence_is_preserved():
    document = "No se requiere gestión de transportistas"
    evidence = "No se requiere gestión de transportistas"

    assert _evidence_in_document(document, evidence)


def test_actual_positive_evidence_is_accepted():
    document = (
        "No se requiere transporte. "
        "Se requiere gestión de devoluciones."
    )

    assert _evidence_in_document(
        document,
        "Se requiere gestión de devoluciones",
    )


def test_markdown_table_formatting_does_not_invalidate_evidence():
    document = (
        "|**Cliente solicitante:**|Logística NovaRetail S.L.|\n"
        "|---|---|\n"
        "|**País del cliente:**|España|"
    )
    assert _evidence_in_document(
        document,
        "Cliente solicitante: Logística NovaRetail S.L.",
    )
    assert _evidence_in_document(
        document,
        "|Cliente solicitante:|Logística NovaRetail S.L.|",
    )
    assert _evidence_in_document(
        document,
        "País del cliente: España",
    )


def test_markdown_table_negative_evidence_is_rejected():
    document = (
        "|**Cliente solicitante:**|Logística NovaRetail S.L.|\n"
        "|---|---|\n"
        "|**País del cliente:**|España|"
    )
    assert not _evidence_in_document(
        document,
        "Cliente solicitante: Empresa Ficticia Inexistente",
    )
    assert not _evidence_in_document(
        document,
        "País del cliente: Francia",
    )
