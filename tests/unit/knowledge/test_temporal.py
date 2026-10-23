from datetime import date

import pytest

from merchant_assistant.knowledge.documents import Document, Section
from merchant_assistant.knowledge.temporal import Period, is_valid_during, reference_period

TODAY = date(2026, 10, 23)


@pytest.mark.parametrize(
    ("query", "start", "end"),
    [
        # The five kit questions that name a month.
        (
            "¿Cuál es el cargo por una entrada de $10.000 en ZT-PRO-2 durante octubre de 2026?",
            date(2026, 10, 1),
            date(2026, 10, 31),
        ),
        (
            "¿En cuántos días hábiles se liquida normalmente un evento finalizado en octubre "
            "de 2026?",
            date(2026, 10, 1),
            date(2026, 10, 31),
        ),
        (
            "¿Cuál era el mínimo mensual de ZT-PRO-2 en julio de 2026?",
            date(2026, 7, 1),
            date(2026, 7, 31),
        ),
        ("¿Cuánto cobraba ZT-START-1 en agosto de 2026?", date(2026, 8, 1), date(2026, 8, 31)),
        (
            "¿Qué documento contiene las tarifas aplicables en octubre de 2026?",
            date(2026, 10, 1),
            date(2026, 10, 31),
        ),
        # The other written forms of a month and a year.
        ("tarifas de octubre del 2026", date(2026, 10, 1), date(2026, 10, 31)),
        ("tarifas octubre 2026", date(2026, 10, 1), date(2026, 10, 31)),
        ("TARIFAS DE FEBRERO DE 2028", date(2028, 2, 1), date(2028, 2, 29)),  # leap year
        ("ventas de setiembre de 2026", date(2026, 9, 1), date(2026, 9, 30)),
        ("ventas de septiembre de 2026", date(2026, 9, 1), date(2026, 9, 30)),
        ("cierre de diciembre de 2026", date(2026, 12, 1), date(2026, 12, 31)),
        # Explicit dates are a single day.
        ("¿qué tarifa regía el 31/08/2026?", date(2026, 8, 31), date(2026, 8, 31)),
        ("¿y el 1/9/2026?", date(2026, 9, 1), date(2026, 9, 1)),
        ("ventas del 2026-09-15", date(2026, 9, 15), date(2026, 9, 15)),
        # A bare year after "en", "de" or "del" is the whole year.
        ("¿qué planes había en 2025?", date(2025, 1, 1), date(2025, 12, 31)),
        ("reglas del 2027", date(2027, 1, 1), date(2027, 12, 31)),
        # Several mentions span from the earliest start to the latest end.
        ("comparar julio de 2026 con octubre de 2026", date(2026, 7, 1), date(2026, 10, 31)),
        ("desde el 15/08/2026 hasta septiembre de 2026", date(2026, 8, 15), date(2026, 9, 30)),
        ("en 2025 y en marzo de 2026", date(2025, 1, 1), date(2026, 3, 31)),
    ],
)
def test_a_period_named_in_the_question_is_the_reference(
    query: str, start: date, end: date
) -> None:
    assert reference_period(query, TODAY) == Period(start, end, "question")


@pytest.mark.parametrize(
    "query",
    [
        # Kit questions that name no period.
        "¿Qué rol puede publicar eventos una vez aprobado?",
        "¿Cuál es el mínimo de entradas por mes del plan ZT-PRO-2?",
        "¿Hasta cuándo puede pedirse devolución por reprogramación?",
        "¿Qué significa que crear_reclamo devuelva REC-000123?",
        "¿Puede un organizador en estado EN_REVISION publicar un evento?",
        # Numbers that are not years, and a month without a year.
        "¿cuánto es el cargo de una entrada de $10.000?",
        "el plan exige 3000 entradas",
        "¿qué pasa en octubre?",
        # An impossible date is not a mention.
        "ventas del 31/02/2026",
    ],
)
def test_a_question_that_names_no_period_is_about_today(query: str) -> None:
    assert reference_period(query, TODAY) == Period(TODAY, TODAY, "today")


def test_the_year_of_a_month_mention_is_not_read_again_as_a_bare_year() -> None:
    # Were "de 2026" read again, the span would be the whole of 2026.
    assert reference_period("en octubre de 2026", TODAY).start == date(2026, 10, 1)


def test_a_period_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="cannot start"):
        Period(date(2026, 2, 1), date(2026, 1, 1), "question")


def document(valid_from: date, valid_until: date) -> Document:
    return Document(
        id="DOC-X",
        title="t",
        kind="tarifario",
        valid_from=valid_from,
        valid_until=valid_until,
        sections=(Section("A", "b"),),
    )


# The historical document of the kit: valid from 2026-01-01 to 2026-08-31.
HISTORICAL = document(date(2026, 1, 1), date(2026, 8, 31))


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 7, 1), date(2026, 7, 31), True),  # inside
        (date(2026, 8, 31), date(2026, 8, 31), True),  # the last valid day
        (date(2026, 9, 1), date(2026, 9, 1), False),  # the day after
        (date(2025, 12, 31), date(2025, 12, 31), False),  # the day before
        (date(2026, 1, 1), date(2026, 1, 1), True),  # the first valid day
        (date(2025, 6, 1), date(2026, 1, 1), True),  # overlaps the start by one day
        (date(2026, 8, 31), date(2026, 10, 31), True),  # overlaps the end by one day
        (date(2025, 1, 1), date(2027, 12, 31), True),  # contains the whole validity
        (date(2026, 10, 1), date(2026, 10, 31), False),  # entirely after
    ],
)
def test_a_document_is_valid_during_a_period_when_the_two_intervals_overlap(
    start: date, end: date, expected: bool
) -> None:
    assert is_valid_during(HISTORICAL, Period(start, end, "question")) is expected
