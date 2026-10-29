"""DESIGN_DECISIONS.md answers what section 8 of the brief asks, in at most one page."""

import re
from pathlib import Path

import pytest

DOCUMENT = Path(__file__).resolve().parents[3] / "DESIGN_DECISIONS.md"
CASE = DOCUMENT.read_text(encoding="utf-8").split("## Caso: Zoco Canchas")[1]
# A page of prose, roughly: the brief allows "máximo una página adicional".
MAX_WORDS = 550


def test_the_canchas_case_fits_in_one_page() -> None:
    assert len(re.findall(r"\S+", CASE)) <= MAX_WORDS


@pytest.mark.parametrize(
    "topic",
    [
        "**Datos a consultar**",
        "**Eventos que evitan la sobreventa.**",
        "**Cálculos que quedan en código**",
        "**Aprobación del dueño antes de ofrecer.**",
        "**Confirmación del cobro.**",
        "**Cómo medir reservas adicionales y margen.**",
    ],
)
def test_it_answers_each_point_the_brief_asks_for(topic: str) -> None:
    assert topic in CASE


def test_the_discount_cap_and_the_approval_stay_in_code() -> None:
    assert "10 %" in CASE
    assert "nunca en el modelo" in CASE
