"""What the model may not say: registrations, unknown documents; and amounts it cannot back."""

from decimal import Decimal

import pytest

from merchant_assistant.agent.domain.output_checks import (
    Amount,
    canonical_doc_id,
    claims_registration,
    money_amounts,
    rewrite_citations,
    unverified_amounts,
)

# --------------------------------------------------------------------------- registration claims


@pytest.mark.parametrize(
    "text",
    [
        "Registré tu reclamo.",
        "Listo, lo registré.",
        "Ya te lo registré con el número que te pasé.",
        "REGISTRÉ EL RECLAMO",
        "registre tu reclamo",  # without the accent
        "Tu reclamo quedó registrado.",
        "El reclamo fue registrado correctamente.",
        "Ya está registrado.",
        "Quedó creado el caso.",
        "Se registró tu reclamo.",
        "Se ha creado el reclamo.",
        "Creé el reclamo por el pago doble.",
        "Generé tu reclamo.",
        "Tu número de reclamo es 1234.",
        "El número de reclamo es REC-000009.",
        "Reclamo registrado con éxito.",
        "Registrado correctamente, te avisamos.",
        "Ha sido abierto un ticket.",
        "No, quedó registrado igual.",  # the negation belongs to another clause
        "No te preocupes, quedó registrado.",
        "I have registered your complaint.",
        "Your complaint has been filed.",
        # A general statement shaped like a claim: over-withheld, the safe side (documented).
        "Un reclamo registrado no significa que esté resuelto.",
    ],
)
def test_a_claim_that_a_complaint_was_registered_is_caught(text: str) -> None:
    assert claims_registration(text)


@pytest.mark.parametrize(
    "text",
    [
        "Para registrar el reclamo necesito el motivo.",
        "¿Querés que lo registre?",
        "Si querés, puedo preparar el reclamo para que lo confirmes.",
        "Todavía no quedó registrado: falta tu confirmación.",
        "No lo registré porque no confirmaste.",
        "El reclamo se registra cuando confirmás los datos.",
        "El plazo de liquidación es de 48 horas [DOC-06].",
        "¿Qué pasó con la entrada?",
        "Revisá el correo y después abrí un reclamo.",  # voseo: an instruction, not a claim
        "Creá el reclamo desde el panel.",
    ],
)
def test_text_that_does_not_claim_a_registration_passes(text: str) -> None:
    assert not claims_registration(text)


# --------------------------------------------------------------------------- citations

KNOWN = frozenset({"DOC-04", "DOC-06"})


@pytest.mark.parametrize(
    ("text", "cleaned", "invalid", "withhold"),
    [
        pytest.param("Es así [DOC-04].", "Es así [DOC-04].", (), False, id="known"),
        pytest.param("Es así [doc-4].", "Es así [DOC-04].", (), False, id="spelled differently"),
        pytest.param("Es así [DOC-99].", "Es así.", ("DOC-99",), False, id="unknown bracketed"),
        pytest.param("Es así (DOC-99).", "Es así.", ("DOC-99",), False, id="unknown in parens"),
        pytest.param(
            "Es así (DOC-04; DOC-99).", "Es así (DOC-04).", ("DOC-99",), False, id="mixed group"
        ),
        pytest.param(
            "Según DOC-99, se cobra a las 48 horas.",
            "Según DOC-99, se cobra a las 48 horas.",
            ("DOC-99",),
            True,
            id="unknown bare mention",
        ),
        pytest.param("Lo dice DOC-06.", "Lo dice DOC-06.", (), False, id="known bare mention"),
        pytest.param(
            "Ver [DOC-04] y DOC-12.", "Ver [DOC-04] y DOC-12.", ("DOC-12",), True, id="both kinds"
        ),
        pytest.param(
            "Es así (DOC-99].", "Es así (DOC-99].", ("DOC-99",), True, id="unbalanced group"
        ),
    ],
)
def test_every_document_mention_must_name_a_retrieved_document(
    text: str, cleaned: str, invalid: tuple[str, ...], withhold: bool
) -> None:
    check = rewrite_citations(text, KNOWN)

    assert (check.text, check.invalid, check.withhold) == (cleaned, invalid, withhold)


def test_a_document_id_has_one_canonical_spelling() -> None:
    assert {canonical_doc_id(raw) for raw in ("DOC-4", "doc-04", "DOC-004")} == {"DOC-04"}
    with pytest.raises(ValueError, match="not a document id"):
        canonical_doc_id("DOC-")


# --------------------------------------------------------------------------- amounts


@pytest.mark.parametrize(
    ("text", "values"),
    [
        pytest.param("Vendiste $45.000.", [Decimal(45000)], id="thousands with a dot"),
        pytest.param("El neto fue $ 45.000,50.", [Decimal("45000.50")], id="decimal comma"),
        pytest.param("Un neto de -15.000 ARS.", [Decimal(15000)], id="negative with suffix"),
        pytest.param("Devolviste $-15.000.", [Decimal(15000)], id="negative after the sign"),
        pytest.param("Son 1500000.00 pesos.", [Decimal(1500000)], id="tool format with suffix"),
        pytest.param("Cuesta ARS 500.", [Decimal(500)], id="ARS prefix"),
        pytest.param(
            "Es $450 + $90 = $540 ARS.",
            [Decimal(450), Decimal(90), Decimal(540)],
            id="several, one with both marks",
        ),
        pytest.param("Vendiste 120 entradas, un 4,5% más.", [], id="no currency mark"),
    ],
)
def test_amounts_of_money_are_read_in_argentine_format(text: str, values: list[Decimal]) -> None:
    assert [amount.value for amount in money_amounts(text)] == values


def test_an_amount_is_unverified_unless_a_payload_has_the_same_figure() -> None:
    known = frozenset({Decimal("1500000.00"), Decimal("15000.00")})

    flagged = unverified_amounts(
        "Bruto $1.500.000, devoluciones -$15.000 y neto $1.490.000.", known
    )

    assert flagged == (Amount("$1.490.000", Decimal(1490000)),)
