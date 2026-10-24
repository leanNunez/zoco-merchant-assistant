"""The relevance floor of buscar_documentacion, calibrated on the kit without the API (F9).

BM25 scores any shared token and a vector search always returns neighbours, so the search
needs a floor to ever say "nothing relevant". This suite loads the real corpus with the
chunking the agent uses, in degraded mode (no key, no network), and pins the calibration:
every kit question is answerable, every off-topic question is `no_data`, and the floor keeps
a margin from both sides.
"""

from datetime import date
from pathlib import Path

import pytest

from merchant_assistant.bootstrap import DOCS_DIR, MIN_BM25_SCORE, SearchBuild, build_search
from merchant_assistant.knowledge.adapters.filesystem import load_questions
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 23)
KIT = load_questions(Path("questions.jsonl"))
# Written for this calibration, before choosing the floor. They share only function words
# ("cuál", "es", "la", "de", "cómo", "se") or nothing at all with the corpus.
OFF_TOPIC = (
    "¿Cuál es la capital de Francia?",
    "¿Quién ganó el mundial 2022?",
    "Receta de empanadas",
    "¿Qué tiempo va a hacer mañana?",
    "¿Cuántos habitantes tiene Japón?",
    "Contame un chiste",
    "¿Cómo se hace un asado?",
    "¿Quién escribió Don Quijote?",
    "¿Cuál es la velocidad de la luz?",
    "Recomendame una película de terror",
    "¿Cómo cambio el aceite del auto?",
    "¿Cuánto mide el monte Everest?",
    "¿Qué es la fotosíntesis?",
    "¿Cuál es el río más largo del mundo?",
    "¿Cómo se juega al ajedrez?",
    "hola",
)
MARGIN = 0.5


@pytest.fixture(scope="module")
def built() -> SearchBuild:
    settings = Settings(_env_file=None, openai_api_key=None, docs_dir=DOCS_DIR)
    return build_search(settings, today=lambda: TODAY)


def test_the_calibration_runs_on_the_twenty_kit_questions(built: SearchBuild) -> None:
    assert len(KIT) == 20
    assert built.mode == "degraded"


@pytest.mark.parametrize("question", KIT, ids=[question.id for question in KIT])
def test_every_kit_question_is_answerable(built: SearchBuild, question: Question) -> None:
    assert built.search.search(question.text).status == "ok"


@pytest.mark.parametrize("question", OFF_TOPIC)
def test_every_off_topic_question_finds_nothing(built: SearchBuild, question: str) -> None:
    assert built.search.search(question).status == "no_data"


def test_the_floor_keeps_a_margin_from_both_distributions(built: SearchBuild) -> None:
    kit_scores = sorted(built.search.best_lexical_score(q.text) for q in KIT)
    off_scores = sorted(built.search.best_lexical_score(q) for q in OFF_TOPIC)

    # The measured numbers this floor was chosen from (ADR-0004): kit 5.871 to 26.707,
    # off-topic 0.000 to 4.626. The floor is their midpoint.
    assert round(kit_scores[0], 3) == 5.871
    assert round(off_scores[-1], 3) == 4.626
    assert kit_scores[0] - MIN_BM25_SCORE >= MARGIN
    assert MIN_BM25_SCORE - off_scores[-1] >= MARGIN


@pytest.mark.parametrize(
    ("query", "status"),
    [
        pytest.param("reserva del carrito", "ok", id="a short query with rare words"),
        pytest.param("cargo por servicio", "ok", id="a short query with a precise term"),
        pytest.param("liquidación", "no_data", id="one common word: below the floor"),
        pytest.param("entrada no llegó", "no_data", id="three common words: below the floor"),
    ],
)
def test_the_known_cost_a_bare_keyword_can_fall_below_the_floor(
    built: SearchBuild, query: str, status: str
) -> None:
    # Not a goal, a limitation on record (SPEC_REVIEW F9): the floor was calibrated on full
    # questions, which the tool description asks the model to send. Pinned so a change shows.
    assert built.search.search(query).status == status
