"""The evaluation pipeline on a three-document kit with a deterministic offline embedder."""

import hashlib
from collections.abc import Sequence
from datetime import date

import numpy as np
import pytest
from evaluation.retrieval.pipeline import (
    MAIN_STRATEGIES,
    REFERENCE_DATE,
    chunkings_to_index,
    run_evaluation,
    texts_to_index,
)
from numpy.typing import NDArray

from merchant_assistant.knowledge.documents import Document, Section
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.temporal import Period
from merchant_assistant.knowledge.text import Tokenizer


class HashingEmbedder:
    """Bag of words hashed into 32 dimensions: no network, and the same text, the same row."""

    model = "hashing-32"

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        rows = np.zeros((len(texts), 32))
        for row, text in zip(rows, texts, strict=True):
            row[0] = 1.0  # a constant component, so no text maps to the zero vector
            for token in Tokenizer()(text):
                row[int(hashlib.sha256(token.encode()).hexdigest(), 16) % 31 + 1] += 1.0
        return rows


MINI_DOCUMENTS = (
    Document(
        id="D-CUR",
        title="Tarifas vigentes",
        kind="tarifario",
        valid_from=date(2026, 9, 1),
        valid_until=date(2027, 3, 31),
        sections=(
            Section("Tarifas", "el plan pro cobra cuatro por ciento por entrada vendida"),
            Section("Vigencia", "aplica desde septiembre de 2026"),
        ),
    ),
    Document(
        id="D-HIS",
        title="Tarifas historicas",
        kind="historico",
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 8, 31),
        sections=(
            Section("Tarifas historicas", "el plan pro cobraba cinco por ciento por entrada"),
            Section("Aviso", "no vigente despues de agosto"),
        ),
    ),
    Document(
        id="D-QR",
        title="Acceso",
        kind="ayuda",
        valid_from=date(2026, 1, 1),
        valid_until=date(2027, 12, 31),
        sections=(
            Section("Acceso", "un qr valido se acepta una sola vez"),
            Section("Offline", "el escaner sincroniza al recuperar conexion"),
        ),
    ),
)

MINI_QUESTIONS = (
    Question("Q1", "¿Cuánto cobra el plan pro?", frozenset({"D-CUR"})),
    Question("Q2", "¿Cuánto cobraba el plan pro en julio de 2026?", frozenset({"D-HIS"})),
    Question("Q3", "¿Cuántas veces se acepta un qr?", frozenset({"D-QR"})),
)


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


GRID_LABELS = [
    "document",
    "section",
    "section+header",
    "window-32-o0",
    "window-32-o5",
    "window-64-o0",
    "window-64-o10",
    "window-128-o0",
    "window-128-o19",
]


def test_the_reference_date_is_fixed_inside_the_validity_of_the_current_tariffs() -> None:
    assert date(2026, 10, 23) == REFERENCE_DATE


def test_the_index_covers_the_grid_and_each_entry_with_its_header_toggled() -> None:
    assert [strategy.label for strategy in chunkings_to_index()] == [
        "document",
        "document+header",
        "section",
        "section+header",
        "window-32-o0",
        "window-32-o0+header",
        "window-32-o5",
        "window-32-o5+header",
        "window-64-o0",
        "window-64-o0+header",
        "window-64-o10",
        "window-64-o10+header",
        "window-128-o0",
        "window-128-o0+header",
        "window-128-o19",
        "window-128-o19+header",
    ]


def test_every_chunk_text_and_every_question_is_sent_to_be_embedded() -> None:
    texts = texts_to_index(MINI_DOCUMENTS, MINI_QUESTIONS)

    # Each mini document is shorter than 32 words: 1 chunk per window strategy.
    # Per header variant: document 3 + section 6 + six window strategies x 3 = 27; x 2 = 54.
    assert len(texts) == 54 + 3
    assert texts[-3:] == [question.text for question in MINI_QUESTIONS]


def test_the_grid_runs_vector_bm25_and_hybrid_on_every_chunking(
    embedder: HashingEmbedder,
) -> None:
    evaluation = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)

    assert [(run.chunking, run.strategy) for run in evaluation.grid] == [
        (label, strategy) for label in GRID_LABELS for strategy in ("vector", "bm25", "hybrid")
    ]


def test_no_chunking_has_a_higher_hybrid_mrr_than_the_selected_one(
    embedder: HashingEmbedder,
) -> None:
    evaluation = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)

    selected = evaluation.selection.selected
    hybrid_mrrs = [run.mrr for run in evaluation.grid if run.strategy == "hybrid"]
    assert max(hybrid_mrrs) == selected.hybrid_mrr
    assert {row.run.chunking for row in evaluation.main} == {selected.label}


def test_the_main_table_has_the_five_strategies_in_order(embedder: HashingEmbedder) -> None:
    evaluation = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)

    assert [row.run.strategy for row in evaluation.main] == list(MAIN_STRATEGIES)


def test_the_filter_drops_the_document_that_was_not_valid_in_the_question_period(
    embedder: HashingEmbedder,
) -> None:
    evaluation = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)

    filtered = next(row.run for row in evaluation.main if row.run.strategy.endswith("(filter)"))
    rankings = {outcome.question_id: outcome.ranking for outcome in filtered.outcomes}
    assert "D-HIS" not in rankings["Q1"]  # today is 2026-10-23
    assert "D-CUR" not in rankings["Q2"]  # the question is about July 2026


def test_the_period_of_each_question_is_recorded(embedder: HashingEmbedder) -> None:
    periods = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder).periods

    assert periods == {
        "Q1": Period(REFERENCE_DATE, REFERENCE_DATE, "today"),
        "Q2": Period(date(2026, 7, 1), date(2026, 7, 31), "question"),
        "Q3": Period(REFERENCE_DATE, REFERENCE_DATE, "today"),
    }


def test_the_ablations_are_stemming_the_header_toggle_and_kind_demotion(
    embedder: HashingEmbedder,
) -> None:
    evaluation = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)
    selected = evaluation.selection.selected.label
    toggled = selected.removesuffix("+header") if "+header" in selected else f"{selected}+header"

    assert [(run.chunking, run.strategy) for run in evaluation.ablations] == [
        (selected, "bm25(stem)"),
        (toggled, "vector"),
        (toggled, "bm25"),
        (toggled, "hybrid"),
        (selected, "hybrid+kind-demotion(historico)"),
    ]
    assert evaluation.chunk_counts[toggled] > 0


def test_the_paired_comparisons_name_what_they_compare(embedder: HashingEmbedder) -> None:
    paired = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder).paired

    assert paired[0].better in ("hybrid+meta(filter)", "hybrid+meta(demote)")
    assert paired[0].baseline == "hybrid"
    assert paired[1].better == "hybrid"
    assert paired[1].baseline in ("bm25", "vector")


def test_two_runs_over_the_same_inputs_give_the_same_evaluation(
    embedder: HashingEmbedder,
) -> None:
    first = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, embedder)
    second = run_evaluation(MINI_DOCUMENTS, MINI_QUESTIONS, HashingEmbedder())

    assert first == second
