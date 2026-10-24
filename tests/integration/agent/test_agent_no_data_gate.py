"""The no-data gate of `buscar_documentacion`, pinned to the committed calibration.

`evaluation/results/retrieval/gate_calibration.md` says which queries the gate must answer and
which it may reject. This suite loads the same queries and the same corpus and cache the
calibration used, builds the agent's search in both modes and checks it says the same, so a
change that makes a legitimate query unanswerable (a new tokenizer, other thresholds, another
chunking) fails here with the query's name. Nothing touches the network: the hybrid mode reads
the committed embeddings cache and any text it does not hold fails loudly.
"""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from evaluation.retrieval.calibration import CalibrationQuery, load_calibration_queries
from numpy.typing import NDArray

from merchant_assistant.bootstrap import (
    AGENT_CHUNKING,
    DOCS_DIR,
    GATE_BM25_DEGRADED,
    GATE_BM25_HYBRID,
    GATE_COSINE_HYBRID,
    SearchBuild,
    build_search,
)
from merchant_assistant.knowledge.adapters.filesystem import load_documents, load_questions
from merchant_assistant.knowledge.adapters.missing_key import MissingKeyEmbedder
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.chunking import chunk_corpus
from merchant_assistant.knowledge.embedding import CachedEmbedder
from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError, InvalidEmbeddingError
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 23)
RESULTS = Path("evaluation/results/retrieval")
CACHE = Path("data/cache/embeddings.sqlite")
QUERIES = load_calibration_queries(
    Path("evaluation/retrieval/calibration_queries.jsonl"), load_questions(Path("questions.jsonl"))
)
LEGITIMATE = [q for q in QUERIES if q.group in ("kit", "short_legit")]
OFF_TOPIC = [q for q in QUERIES if q.group == "off_topic"]


def settings() -> Settings:
    return Settings(_env_file=None, openai_api_key=None, docs_dir=DOCS_DIR)


@pytest.fixture(scope="module")
def degraded() -> SearchBuild:
    built = build_search(settings(), today=lambda: TODAY)
    assert (built.mode, built.reason) == ("degraded", "no_key")
    return built


@pytest.fixture(scope="module")
def hybrid() -> SearchBuild:
    """The selected search over the committed cache; an uncached text raises, never the API."""
    config = settings()
    embedder = CachedEmbedder(
        MissingKeyEmbedder(config.embeddings_model), SqliteEmbeddingCache(CACHE)
    )
    built = build_search(config, today=lambda: TODAY, embedder=embedder)
    assert (built.mode, built.reason) == ("hybrid", "complete")
    return built


def ids(queries: list[CalibrationQuery]) -> list[str]:
    return [query.id for query in queries]


# --- the thresholds are the calibration's ----------------------------------------------------


def test_the_thresholds_in_the_code_are_the_ones_the_committed_calibration_chose() -> None:
    chosen = json.loads((RESULTS / "gate_calibration.json").read_text(encoding="utf-8"))

    assert chosen["selected_tokenizer"] == "default"
    assert chosen["thresholds"] == {
        "bm25_degraded": GATE_BM25_DEGRADED,
        "bm25_hybrid": GATE_BM25_HYBRID,
        "cosine_hybrid": GATE_COSINE_HYBRID,
    }


def test_the_agent_chunking_is_the_one_the_declared_selection_picked() -> None:
    metrics = json.loads((RESULTS / "metrics.json").read_text(encoding="utf-8"))

    assert metrics["selected_chunking"]["label"] == AGENT_CHUNKING.label == "document"
    assert len(chunk_corpus(load_documents(DOCS_DIR), AGENT_CHUNKING)) == 12


# --- every legitimate query is answerable, in both modes -----------------------------------


@pytest.mark.parametrize("query", LEGITIMATE, ids=ids(LEGITIMATE))
def test_degraded_mode_answers_every_kit_and_short_legitimate_query(
    degraded: SearchBuild, query: CalibrationQuery
) -> None:
    assert degraded.search.search(query.text).status == "ok"


@pytest.mark.parametrize("query", LEGITIMATE, ids=ids(LEGITIMATE))
def test_hybrid_mode_answers_every_kit_and_short_legitimate_query(
    hybrid: SearchBuild, query: CalibrationQuery
) -> None:
    outcome = hybrid.search.search(query.text)

    assert outcome.status == "ok"
    assert outcome.degraded is None  # the cache held the query: no fallback happened


def test_the_three_queries_that_fell_below_the_old_floor_are_answered_in_both_modes(
    degraded: SearchBuild, hybrid: SearchBuild
) -> None:
    for text in (
        "¿Cómo transfiero una entrada?",
        "Quiero hacer un reclamo.",
        "No me liquidaron un evento, quiero hacer un reclamo.",
    ):
        assert degraded.search.search(text).status == "ok"
        assert hybrid.search.search(text).status == "ok"


def test_the_document_a_short_question_is_about_comes_back_first_in_hybrid_mode(
    hybrid: SearchBuild,
) -> None:
    found = hybrid.search.search("¿Cómo transfiero una entrada?")

    assert found.passages[0].doc_id == "DOC-09"


# --- what the gate rejects, as measured ----------------------------------------------------


def rejected(built: SearchBuild, queries: list[CalibrationQuery]) -> list[str]:
    return [q.id for q in queries if built.search.search(q.text).status == "no_data"]


def test_hybrid_mode_rejects_33_of_the_45_off_topic_queries_and_3_of_the_13_hard_ones(
    hybrid: SearchBuild,
) -> None:
    gone = rejected(hybrid, OFF_TOPIC)

    assert len(gone) == 33
    assert len([q for q in OFF_TOPIC if q.hard and q.id in gone]) == 3
    # The 12 that pass are the ones that look like the domain to the vector, as the report lists.
    assert sorted(set(ids(OFF_TOPIC)) - set(gone)) == [
        "N23", "N27", "N33", "N34", "N35", "N36", "N37", "N40", "N42", "N43", "N44", "N45",
    ]  # fmt: skip


def test_degraded_mode_rejects_only_the_queries_that_share_no_word_with_the_corpus(
    degraded: SearchBuild,
) -> None:
    assert rejected(degraded, OFF_TOPIC) == ["N11", "N31"]  # "hola" and "gracias"


def test_the_known_cost_of_the_degraded_gate_is_that_an_off_topic_question_passes(
    degraded: SearchBuild, hybrid: SearchBuild
) -> None:
    # Recorded on purpose (ADR-0007): without embeddings the rule keeps every legitimate query
    # answerable, so a question that only shares "el", "de" or "la" with the corpus is let through.
    question = "¿Cuál es la capital de Australia?"  # N01 of the calibration queries

    assert degraded.search.search(question).status == "ok"
    in_hybrid = hybrid.search.search(question)
    assert (in_hybrid.status, in_hybrid.degraded) == ("no_data", None)


@pytest.mark.parametrize("greeting", ["hola", "gracias", "zzzqx wvvkj"])
def test_a_query_with_no_word_in_common_is_no_data_in_both_modes(
    degraded: SearchBuild, hybrid: SearchBuild, greeting: str
) -> None:
    assert degraded.search.search(greeting).status == "no_data"
    if greeting != "zzzqx wvvkj":  # the others are cached; the gibberish would need the API
        assert hybrid.search.search(greeting).status == "no_data"


def test_the_margins_to_the_thresholds_are_narrow_and_they_are_on_record() -> None:
    # The weakest legitimate cosine is 0.3787 ("qué es un lote") and the strongest rejected
    # off-topic one is 0.3539: the threshold sits in between with about 0.012 on each side.
    assert 0.3539 < GATE_COSINE_HYBRID <= 0.3787
    assert pytest.approx(0.0121, abs=1e-3) == GATE_COSINE_HYBRID - 0.3539
    assert pytest.approx(0.0127, abs=1e-3) == 0.3787 - GATE_COSINE_HYBRID


# --- the provider failing on a new query ---------------------------------------------------


class ScriptedEmbedder:
    """Corpus texts get one basis vector each; a query gets the vector it was given (or fails).

    Thirteen dimensions: twelve chunks and one the chunks never use, so a query along it is
    orthogonal to the whole corpus.
    """

    model = "fake-13"

    def __init__(self, corpus_texts: list[str]) -> None:
        self._corpus = {text: position for position, text in enumerate(corpus_texts)}
        self.queries: dict[str, NDArray[np.float64]] = {}
        self.failure: Exception | None = None
        self.query_calls = 0

    def basis(self, position: int) -> NDArray[np.float64]:
        vector = np.zeros(13)
        vector[position] = 1.0
        return vector

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        rows = []
        for text in texts:
            if text in self._corpus:
                rows.append(self.basis(self._corpus[text]))
                continue
            self.query_calls += 1
            if self.failure is not None:
                raise self.failure
            rows.append(self.queries[text])
        return np.stack(rows)


@pytest.fixture
def scripted() -> tuple[SearchBuild, ScriptedEmbedder]:
    chunks = chunk_corpus(load_documents(DOCS_DIR), AGENT_CHUNKING)
    embedder = ScriptedEmbedder([chunk.index_text for chunk in chunks])
    return build_search(settings(), today=lambda: TODAY, embedder=embedder), embedder


GIBBERISH = "zzzqx wvvkj"  # shares no word with the corpus: BM25 alone says nothing


def test_a_phrase_with_no_lexical_match_but_a_close_vector_is_answered(
    scripted: tuple[SearchBuild, ScriptedEmbedder],
) -> None:
    built, embedder = scripted
    embedder.queries[GIBBERISH] = embedder.basis(3)  # the chunk of DOC-04

    found = built.search.search(GIBBERISH)

    assert found.status == "ok"
    assert found.passages[0].doc_id == "DOC-04"
    assert found.degraded is None


def test_a_phrase_far_from_every_chunk_and_without_lexical_match_is_no_data(
    scripted: tuple[SearchBuild, ScriptedEmbedder],
) -> None:
    built, embedder = scripted
    embedder.queries[GIBBERISH] = embedder.basis(12)  # orthogonal to the corpus

    found = built.search.search(GIBBERISH)

    assert (found.status, found.passages, found.degraded) == ("no_data", (), None)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (EmbeddingsUnavailableError("the provider is down"), "embeddings_unavailable"),
        (InvalidEmbeddingError("zero vector"), "invalid_embedding"),
    ],
)
def test_when_embedding_the_query_fails_the_query_is_answered_by_bm25_and_flagged(
    scripted: tuple[SearchBuild, ScriptedEmbedder], failure: Exception, code: str
) -> None:
    built, embedder = scripted
    embedder.failure = failure

    found = built.search.search("¿cuánto dura la reserva del carrito?")

    assert found.status == "ok"
    assert found.passages[0].doc_id == "DOC-04"
    assert found.degraded == code


def test_the_fallback_uses_the_degraded_gate_and_still_says_why(
    scripted: tuple[SearchBuild, ScriptedEmbedder],
) -> None:
    built, embedder = scripted
    embedder.failure = EmbeddingsUnavailableError("the provider is down")

    found = built.search.search(GIBBERISH)

    assert (found.status, found.degraded) == ("no_data", "embeddings_unavailable")


def test_a_failure_hurts_only_its_own_query(
    scripted: tuple[SearchBuild, ScriptedEmbedder],
) -> None:
    built, embedder = scripted
    embedder.queries[GIBBERISH] = embedder.basis(3)
    embedder.failure = EmbeddingsUnavailableError("blip")
    assert built.search.search(GIBBERISH).degraded == "embeddings_unavailable"

    embedder.failure = None

    again = built.search.search(GIBBERISH)
    assert (again.status, again.degraded) == ("ok", None)


def test_a_bug_in_the_embedder_is_not_swallowed_as_a_provider_failure(
    scripted: tuple[SearchBuild, ScriptedEmbedder],
) -> None:
    built, embedder = scripted
    embedder.failure = RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        built.search.search("¿cuánto dura la reserva del carrito?")
