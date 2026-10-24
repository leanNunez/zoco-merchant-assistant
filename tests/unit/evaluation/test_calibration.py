"""The no-data gate selection on scores written by hand, so every threshold is computable."""

import json
from pathlib import Path

import pytest
from evaluation.retrieval.calibration import (
    CalibrationQuery,
    Group,
    KitRetrieval,
    QueryScores,
    answers,
    calibrate,
    calibration_texts,
    choose_gate,
    confusion,
    load_calibration_queries,
)
from hypothesis import given
from hypothesis import strategies as st

from merchant_assistant.knowledge.errors import QuestionFormatError
from merchant_assistant.knowledge.questions import Question

KIT = (Question("Q01", "¿Cuánto demora el alta?", frozenset({"DOC-02"})),)


def point(
    query_id: str, group: Group, bm25: float, cosine: float = 0.0, *, stopwords: float | None = None
) -> QueryScores:
    query = CalibrationQuery(query_id, group, f"texto {query_id}", hard=False)
    return QueryScores(
        query=query,
        bm25=bm25,
        bm25_stopwords=bm25 if stopwords is None else stopwords,
        cosine=cosine,
        bm25_top="",
        cosine_top="",
        previous_bm25=bm25,
    )


# --- the degraded gate (BM25 alone) --------------------------------------------------------


def test_the_degraded_threshold_is_the_middle_of_the_cell_between_rejected_and_kept() -> None:
    scored = [
        point("Q01", "kit", 10.0),
        point("L01", "short_legit", 6.0),
        point("N01", "off_topic", 1.0),
        point("N02", "off_topic", 4.0),
        point("N03", "off_topic", 7.0),
    ]

    gate = choose_gate(scored, "default", hybrid=False)

    # N01 and N02 sit below the weakest legitimate query (6); N03 (7) cannot be rejected.
    assert (gate.bm25.value, gate.rejected_off_topic, gate.relaxed) == (5.0, 2, False)
    assert (gate.bm25.low, gate.bm25.high) == (4.0, 6.0)
    assert gate.cosine is None
    assert gate.missed == ()


def test_an_off_topic_query_that_ties_with_the_weakest_legitimate_one_is_not_rejected() -> None:
    scored = [
        point("Q01", "kit", 8.0),
        point("L01", "short_legit", 3.0),
        point("N01", "off_topic", 3.0),
        point("N02", "off_topic", 1.0),
    ]

    gate = choose_gate(scored, "default", hybrid=False)

    assert (gate.bm25.value, gate.rejected_off_topic) == (2.0, 1)
    assert [answers(s, gate, "default") for s in scored] == [True, True, True, False]


def test_when_keeping_every_legitimate_query_rejects_nothing_the_kit_alone_is_kept() -> None:
    scored = [
        point("Q01", "kit", 5.0),
        point("L01", "short_legit", 0.0),  # shares no word with the corpus
        point("N01", "off_topic", 1.0),
        point("N02", "off_topic", 2.0),
    ]

    gate = choose_gate(scored, "default", hybrid=False)

    assert (gate.bm25.value, gate.rejected_off_topic, gate.relaxed) == (3.5, 2, True)
    assert gate.missed == ("L01",)


def test_without_any_off_topic_below_the_kit_the_gate_rejects_nothing() -> None:
    scored = [
        point("Q01", "kit", 1.0),
        point("N01", "off_topic", 2.0),
        point("N02", "off_topic", 3.0),
    ]

    gate = choose_gate(scored, "default", hybrid=False)

    assert (gate.rejected_off_topic, gate.bm25.value) == (0, 1.0)
    assert all(answers(s, gate, "default") for s in scored)


def test_the_stopword_scores_are_the_ones_used_when_that_tokenizer_is_chosen() -> None:
    scored = [
        point("Q01", "kit", 4.0, stopwords=6.0),
        point("N01", "off_topic", 5.0, stopwords=1.0),
    ]

    assert choose_gate(scored, "default", hybrid=False).rejected_off_topic == 0
    stopped = choose_gate(scored, "stopwords", hybrid=False)
    assert (stopped.bm25.value, stopped.rejected_off_topic) == (3.5, 1)


# --- the hybrid gate (BM25 and cosine) -----------------------------------------------------


HYBRID = [
    point("Q01", "kit", 1.0, 0.8),  # saved by the cosine
    point("L01", "short_legit", 9.0, 0.2),  # saved by BM25
    point("N01", "off_topic", 2.0, 0.1),
    point("N02", "off_topic", 8.0, 0.15),
    point("N03", "off_topic", 3.0, 0.9),  # looks like the domain to the vector: cannot be rejected
]


def test_the_hybrid_gate_rejects_only_what_is_weak_in_both_signals() -> None:
    gate = choose_gate(HYBRID, "default", hybrid=True)

    # BM25 below 9 (the last value that does not reject L01) and a cosine in (0.15, 0.8].
    assert gate.rejected_off_topic == 2
    assert gate.bm25.value == 8.5
    assert (gate.bm25.low, gate.bm25.high) == (8.0, 9.0)
    assert gate.cosine is not None
    assert (gate.cosine.low, gate.cosine.high) == (0.2, 0.8)
    assert gate.cosine.value == 0.5
    assert gate.tied_cells == 2
    assert [answers(s, gate, "default") for s in HYBRID] == [True, True, False, False, True]


def test_the_hybrid_gate_never_rejects_a_query_either_signal_would_rescue() -> None:
    gate = choose_gate(HYBRID, "default", hybrid=True)

    strong_bm25 = point("X", "off_topic", gate.bm25.value + 1.0, 0.0)
    strong_cosine = point("Y", "off_topic", 0.0, (gate.cosine.value if gate.cosine else 0) + 0.01)
    assert answers(strong_bm25, gate, "default")
    assert answers(strong_cosine, gate, "default")


def test_the_thresholds_are_rounded_only_as_far_as_the_cell_allows() -> None:
    scored = [
        point("Q01", "kit", 10.0, 0.5),
        point("N01", "off_topic", 1.0000004, 0.1),
        point("N02", "off_topic", 1.0000006, 0.1),
    ]

    gate = choose_gate(scored, "default", hybrid=False)

    # The cell between the two off-topic scores is only 2e-7 wide: three decimals would leave it.
    assert gate.bm25.low < gate.bm25.value <= gate.bm25.high


# --- properties ----------------------------------------------------------------------------

scores = st.floats(min_value=0.0, max_value=30.0, allow_nan=False, allow_infinity=False)
groups = st.sampled_from(["kit", "short_legit", "off_topic"])


@given(st.lists(st.tuples(groups, scores, scores), min_size=3, max_size=25), st.booleans())
def test_the_chosen_gate_always_keeps_every_kit_query(
    rows: list[tuple[Group, float, float]], hybrid: bool
) -> None:
    scored = [point(f"P{n:02d}", g, b, c / 30) for n, (g, b, c) in enumerate(rows)]
    if not any(s.query.group == "kit" for s in scored):
        scored.append(point("Q00", "kit", 5.0, 0.5))

    gate = choose_gate(scored, "default", hybrid=hybrid)

    assert all(answers(s, gate, "default") for s in scored if s.query.group == "kit")
    if not gate.relaxed:
        assert all(answers(s, gate, "default") for s in scored if s.query.group != "off_topic")


@given(st.lists(st.tuples(groups, scores, scores), min_size=3, max_size=25))
def test_the_reported_missed_queries_are_exactly_the_legitimate_ones_the_gate_rejects(
    rows: list[tuple[Group, float, float]],
) -> None:
    scored = [point(f"P{n:02d}", g, b, c / 30) for n, (g, b, c) in enumerate(rows)]
    scored.append(point("Q00", "kit", 5.0, 0.5))

    gate = choose_gate(scored, "default", hybrid=True)

    rejected = tuple(
        s.query.id
        for s in scored
        if s.query.group != "off_topic" and not answers(s, gate, "default")
    )
    assert gate.missed == rejected
    off_rejected = sum(
        not answers(s, gate, "default") for s in scored if s.query.group == "off_topic"
    )
    assert gate.rejected_off_topic == off_rejected


# --- confusion and the stopword decision ---------------------------------------------------


def test_the_confusion_table_counts_each_group_and_the_hard_negatives() -> None:
    hard = QueryScores(
        CalibrationQuery("N02", "off_topic", "entrada del cine", hard=True),
        2.0,
        2.0,
        0.0,
        "",
        "",
        2.0,
    )
    scored = [
        point("Q01", "kit", 10.0),
        point("L01", "short_legit", 6.0),
        point("N01", "off_topic", 1.0),
        hard,
    ]

    gate = choose_gate(scored, "default", hybrid=False)
    table = confusion(scored, gate, "default")

    assert (table.kit_answered, table.kit_total) == (1, 1)
    assert (table.short_answered, table.short_total) == (1, 1)
    assert (table.off_rejected, table.off_total) == (2, 2)
    assert (table.hard_rejected, table.hard_total) == (1, 1)


def margins(default_weakest: float, stop_weakest: float) -> list[QueryScores]:
    return [
        point("Q01", "kit", default_weakest, stopwords=stop_weakest),
        point("N01", "off_topic", 1.0, stopwords=1.0),
    ]


SAME = KitRetrieval(0.975, 1.0)


def test_the_stopword_tokenizer_is_adopted_only_if_retrieval_holds_and_the_margin_grows() -> None:
    adopted = calibrate(margins(5.0, 9.0), SAME, SAME, chunk_count=12)

    assert (adopted.decision.adopted, adopted.selected) == (True, "stopwords")
    assert adopted.decision.margin_improves


@pytest.mark.parametrize(
    ("stopwords_kit", "reason"),
    [
        (KitRetrieval(0.95, 1.0), "recall"),
        (KitRetrieval(0.975, 0.95), "mrr"),
    ],
)
def test_a_drop_in_kit_retrieval_keeps_the_default_tokenizer(
    stopwords_kit: KitRetrieval, reason: str
) -> None:
    result = calibrate(margins(5.0, 9.0), SAME, stopwords_kit, chunk_count=12)

    assert (result.decision.adopted, result.selected) == (False, "default")
    assert result.decision.margin_improves
    assert (result.decision.recall_not_lower, result.decision.mrr_not_lower) == (
        reason != "recall",
        reason != "mrr",
    )


def test_a_margin_that_does_not_grow_keeps_the_default_tokenizer() -> None:
    result = calibrate(margins(5.0, 5.0), SAME, SAME, chunk_count=12)

    assert (result.decision.adopted, result.decision.margin_improves) == (False, False)


def test_a_margin_that_cannot_be_normalized_never_counts_as_an_improvement() -> None:
    # The weakest legitimate query shares no token with the corpus once stopwords are gone: its
    # score is 0, so (L - U) / L is undefined and cannot be said to have improved.
    result = calibrate(margins(0.5, 0.0), SAME, SAME, chunk_count=12)

    assert result.results["stopwords"].relative_margin is None
    assert result.decision.margin_improves is False
    assert result.selected == "default"


def test_the_previous_floor_is_applied_to_the_scores_on_its_own_chunking() -> None:
    previous = point("L01", "short_legit", 9.0)
    scored = [point("Q01", "kit", 9.0), QueryScores(previous.query, 9.0, 9.0, 0.0, "", "", 4.0)]
    scored.append(point("N01", "off_topic", 1.0))

    result = calibrate(scored, SAME, SAME, chunk_count=12)

    assert result.previous_floor_misses == ("L01",)
    assert result.previous_floor_off_accepted == 0


# --- the committed queries and the loader --------------------------------------------------

QUERIES = Path("evaluation/retrieval/calibration_queries.jsonl")


def write(path: Path, *rows: object) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", "utf-8")
    return path


KIT_ROW = {"group": "kit", "source": "questions.jsonl"}


def test_the_kit_row_expands_to_the_kit_questions_without_copying_them(tmp_path: Path) -> None:
    path = write(
        tmp_path / "q.jsonl",
        KIT_ROW,
        {"id": "L01", "group": "short_legit", "text": "cuánto cobran", "expected": ["DOC-05"]},
        {"id": "N01", "group": "off_topic", "text": "hola"},
        {"id": "N02", "group": "off_topic", "text": "entrada del cine", "hard": True},
    )

    queries = load_calibration_queries(path, KIT)

    assert [(q.id, q.group, q.text, q.hard) for q in queries] == [
        ("Q01", "kit", "¿Cuánto demora el alta?", False),
        ("L01", "short_legit", "cuánto cobran", False),
        ("N01", "off_topic", "hola", False),
        ("N02", "off_topic", "entrada del cine", True),
    ]
    assert queries[0].expected == frozenset({"DOC-02"})
    assert queries[1].expected == frozenset({"DOC-05"})
    assert calibration_texts(queries)[0] == "¿Cuánto demora el alta?"


@pytest.mark.parametrize(
    ("rows", "fragment"),
    [
        ([{"id": "L01", "group": "short_legit", "text": "x"}], "kit row is missing"),
        ([KIT_ROW, KIT_ROW], "one kit row"),
        ([{"group": "kit", "source": "other.jsonl"}], "one kit row"),
        ([KIT_ROW, {"id": "X", "group": "weird", "text": "x"}], "unknown group"),
        ([KIT_ROW, {"id": "N01", "group": "off_topic", "text": " "}], "an id and a text"),
        ([KIT_ROW, {"group": "off_topic", "text": "hola"}], "an id and a text"),
        (
            [KIT_ROW, {"id": "N01", "group": "off_topic", "text": "hola", "expected": ["DOC-01"]}],
            "no expected document",
        ),
        (
            [KIT_ROW, {"id": "L01", "group": "short_legit", "text": "x", "hard": True}],
            "only off-topic",
        ),
        (
            [KIT_ROW, {"id": "N01", "group": "off_topic", "text": "a"},
             {"id": "N01", "group": "off_topic", "text": "b"}],
            "repeated query ids",
        ),
        (
            [KIT_ROW, {"id": "L01", "group": "short_legit", "text": "x", "expected": "DOC-01"}],
            "list of document ids",
        ),
    ],
)  # fmt: skip
def test_a_malformed_query_file_is_refused_with_its_reason(
    tmp_path: Path, rows: list[object], fragment: str
) -> None:
    with pytest.raises(QuestionFormatError) as refused:
        load_calibration_queries(write(tmp_path / "q.jsonl", *rows), KIT)

    assert fragment in refused.value.detail


def test_a_line_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('{"group": "kit", "source": "questions.jsonl"}\nnot json\n', encoding="utf-8")

    with pytest.raises(QuestionFormatError) as refused:
        load_calibration_queries(path, KIT)

    assert "q.jsonl:2" in refused.value.detail


def test_the_committed_queries_have_the_groups_the_calibration_needs() -> None:
    from merchant_assistant.knowledge.adapters.filesystem import load_questions

    kit = load_questions(Path("questions.jsonl"))
    queries = load_calibration_queries(QUERIES, kit)

    by_group = {
        g: [q for q in queries if q.group == g] for g in ("kit", "short_legit", "off_topic")
    }
    assert len(by_group["kit"]) == 20
    assert len(by_group["short_legit"]) >= 24
    assert len(by_group["off_topic"]) >= 30
    assert sum(q.hard for q in by_group["off_topic"]) >= 6
    folded = [q.text.casefold() for q in queries]
    assert len(set(folded)) == len(folded)  # no query twice, so none is copied from the kit
    assert all(q.expected for q in by_group["short_legit"])  # every one names the document


def test_the_three_queries_the_review_started_from_are_in_the_set() -> None:
    from merchant_assistant.knowledge.adapters.filesystem import load_questions

    queries = load_calibration_queries(QUERIES, load_questions(Path("questions.jsonl")))
    texts = {q.text for q in queries if q.group == "short_legit"}

    assert {
        "¿Cómo transfiero una entrada?",
        "Quiero hacer un reclamo.",
        "No me liquidaron un evento, quiero hacer un reclamo.",
    } <= texts
