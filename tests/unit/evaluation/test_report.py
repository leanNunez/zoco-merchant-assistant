"""The saved files, rendered from an evaluation built by hand so every cell is known."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date

from evaluation.retrieval.pipeline import (
    MAIN_STRATEGIES,
    AblationDelta,
    MainRow,
    PairedComparison,
    RetrievalEvaluation,
)
from evaluation.retrieval.provenance import Provenance
from evaluation.retrieval.report import to_json, to_markdown
from evaluation.retrieval.scoring import Interval, StrategyRun, score_question
from evaluation.retrieval.selection import ChunkingCandidate, Selection
from evaluation.retrieval.tiebreak import Metrics, TieBreakRow

from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.temporal import Period

QUESTIONS = (
    Question("QA", "¿Cuánto cobra el plan | pro?", frozenset({"D1"})),
    Question(
        "QB",
        "¿Qué dos documentos hablan del alta y de la publicación de eventos?",
        frozenset({"D1", "D2"}),
    ),
)
TODAY = date(2026, 10, 23)


def run(name: str, rankings: Mapping[str, Sequence[str]], chunking: str = "section") -> StrategyRun:
    outcomes = tuple(score_question(rankings[q.id], q) for q in QUESTIONS)
    return StrategyRun(name, chunking, 4, outcomes)


# Every strategy finds both documents of QB except bm25 (half) and the filter (none).
RANKINGS: dict[str, dict[str, list[str]]] = {
    "vector": {"QA": ["D3", "D4", "D5", "D1"], "QB": ["D1", "D2"]},
    "bm25": {"QA": ["D1", "D3"], "QB": ["D1", "D3", "D4", "D2"]},
    "hybrid": {"QA": ["D1", "D3"], "QB": ["D2", "D1"]},
    "hybrid+meta(filter)": {"QA": ["D1"], "QB": []},
    "hybrid+meta(demote)": {"QA": ["D1", "D3"], "QB": ["D2", "D1"]},
}


DELTAS = (
    AblationDelta("section", "bm25(stem)", "section", "bm25", 0.0, 0.0, 0.0, (), ()),
    AblationDelta("section", "bm25(stopwords)", "section", "bm25", 0.25, 0.0, 0.25, ("QB",), ()),
    AblationDelta(
        "section", "hybrid(stopwords)", "section", "hybrid", -0.5, 0.0, 0.0, ("QA",), ("QB",)
    ),
)


def evaluation() -> RetrievalEvaluation:
    main = tuple(
        MainRow(run(name, RANKINGS[name]), Interval(0.25, 1.0), Interval(0.125, 1.0))
        for name in MAIN_STRATEGIES
    )
    return RetrievalEvaluation(
        grid=tuple(row.run for row in main[:3]),
        selection=Selection(
            ChunkingCandidate("section", "section", 4, 1.0, 1.0, 1), "`section` gana."
        ),
        main=main,
        paired=(
            PairedComparison(
                "meta menos híbrido", "hybrid+meta(demote)", "hybrid", -0.0, Interval(-0.0, 0.0)
            ),
        ),
        ablations=(run("bm25(stem)", RANKINGS["bm25"]),),
        periods={
            "QA": Period(TODAY, TODAY, "today"),
            "QB": Period(date(2026, 7, 1), date(2026, 7, 31), "question"),
        },
        chunk_counts={"section": 4},
        ablation_deltas=DELTAS,
    )


PROVENANCE = Provenance("text-embedding-3-small", TODAY, "abc123", {"section": 4}, 0)


def markdown() -> str:
    return to_markdown(evaluation(), PROVENANCE, QUESTIONS)


def row_of(question_id: str) -> str:
    return next(line for line in markdown().splitlines() if line.startswith(f"| {question_id}"))


def test_the_selection_rule_is_written_before_any_table() -> None:
    text = markdown()

    assert text.index("## Reglas declaradas antes de ver las tablas") < text.index("| Chunking")


def test_the_report_says_how_much_one_question_weighs() -> None:
    # Two questions: one of them is half of every mean.
    assert "**una sola pregunta mueve un promedio 50 puntos** (0.50 en recall@3" in markdown()


def test_the_provenance_is_in_the_report_without_any_timestamp() -> None:
    text = markdown()

    assert "- Modelo de embeddings: `text-embedding-3-small`" in text
    assert "`abc123`" in text
    assert "- Llamadas a la API de embeddings durante esta corrida: 0" in text
    assert "2026-10-23" in text
    assert text.count("2026-10-") == 1  # the reference date, and no generation date


def test_a_question_some_strategy_misses_is_marked_and_names_who_misses_it() -> None:
    # QA: vector ranks D1 fourth (out of the top 3); everyone else has it first.
    assert row_of("QA") == (
        "| QA ✗ | ¿Cuánto cobra el plan / pro? | D1 | hoy | 4 · D3, D4, D5 | 1 · D1, D3 "
        "| 1 · D1, D3 | 1 · D1 | 1 · D1, D3 | vector |"
    )


def test_a_partial_miss_and_an_empty_result_are_told_apart() -> None:
    row = row_of("QB")

    assert row.startswith("| QB ✗ | ¿Qué dos documentos hablan del alta y de la publicación de… ")
    assert "| 2026-07-01 a 2026-07-31 |" in row
    assert "| — · (vacío) |" in row
    assert row.endswith("| bm25 (parcial), hybrid+meta(filter) |")


def test_the_main_table_shows_each_metric_with_its_interval() -> None:
    # hybrid: QA rank 1, QB rank 1 -> recall 1, hit 1, MRR 1
    assert "| hybrid | 1.000 | [0.250, 1.000] | 1.000 | 1.000 | [0.125, 1.000] |" in markdown()


def test_the_json_holds_the_same_numbers_and_the_per_question_detail() -> None:
    data = json.loads(to_json(evaluation(), PROVENANCE, QUESTIONS))

    assert data["provenance"]["api_calls"] == 0
    assert data["provenance"]["reference_date"] == "2026-10-23"
    assert data["selected_chunking"] == {"label": "section", "reason": "`section` gana."}
    # bm25: QA recall 1, QB recall 1/2 -> 0.75; MRR (1 + 1) / 2 = 1
    bm25 = next(row for row in data["main"] if row["strategy"] == "bm25")
    assert (bm25["recall_at_3"], bm25["hit_at_3"], bm25["mrr"]) == (0.75, 1.0, 1.0)
    assert bm25["mrr_ci95"] == [0.125, 1.0]
    qa = data["per_question"][0]
    assert qa["reference_period"] == {"start": "2026-10-23", "end": "2026-10-23", "source": "today"}
    assert list(qa["strategies"]) == list(MAIN_STRATEGIES)
    assert qa["strategies"]["vector"] == {
        "first_relevant_rank": 4,
        "top3": ["D3", "D4", "D5"],
        "recall_at_3": 0.0,
        "hit_at_3": 0.0,
        "reciprocal_rank": 0.25,
    }


def test_a_negative_zero_is_saved_as_a_plain_zero() -> None:
    text = to_json(evaluation(), PROVENANCE, QUESTIONS)

    assert "-0.0" not in text
    assert json.loads(text)["paired_differences"][0]["mean_difference"] == 0.0


def test_the_report_says_which_questions_each_variant_moved() -> None:
    text = markdown()

    assert (
        "| `section` bm25(stopwords) | `section` bm25 | +0.250 | +0.000 | +0.250 | QB | — |" in text
    )
    assert (
        "| `section` hybrid(stopwords) | `section` hybrid | -0.500 | +0.000 | +0.000 | QA | QB |"
        in text
    )


def test_the_report_groups_the_variants_by_how_many_questions_they_moved() -> None:
    text = markdown()

    assert "- No mueven ninguna de las 2 preguntas: `section` bm25(stem)." in text
    assert "- Mueven exactamente una pregunta: `section` bm25(stopwords)." in text
    assert "- Mueven dos o más preguntas: `section` hybrid(stopwords)." in text
    assert "**una diferencia de una pregunta no es evidencia**" in text


def test_the_json_keeps_the_ablation_deltas() -> None:
    data = json.loads(to_json(evaluation(), PROVENANCE, QUESTIONS))

    assert data["ablation_deltas"][1] == {
        "chunking": "section",
        "strategy": "bm25(stopwords)",
        "reference_chunking": "section",
        "reference": "bm25",
        "recall_at_3_delta": 0.25,
        "hit_at_3_delta": 0.0,
        "mrr_delta": 0.25,
        "improved": ["QB"],
        "worsened": [],
    }


TIE_BREAK = (
    TieBreakRow(
        "hybrid",
        "section",
        default=Metrics(1.0, 1.0, 0.75),
        reversed=Metrics(1.0, 1.0, 0.5),
        expected=Metrics(1.0, 1.0, 0.625),
        worst_mrr=0.5,
        best_mrr=0.75,
        with_ties=("QA", "QB"),
        first_two_depend=("QA", "QB"),
        outcome_depends=("QA",),
    ),
    TieBreakRow(
        "hybrid+meta(demote)",
        "section",
        default=Metrics(1.0, 1.0, 0.75),
        reversed=Metrics(1.0, 1.0, 0.75),
        expected=Metrics(1.0, 1.0, 0.75),
        worst_mrr=0.75,
        best_mrr=0.75,
        with_ties=("QA", "QB"),
        first_two_depend=("QB",),
        outcome_depends=(),
    ),
)


def with_tie_break() -> str:
    return to_markdown(replace(evaluation(), tie_break=TIE_BREAK), PROVENANCE, QUESTIONS)


def test_the_tie_break_section_gives_each_variant_and_the_worst_and_best_order() -> None:
    text = with_tie_break()

    assert "## Sensibilidad al desempate de RRF" in text
    assert "| hybrid | por defecto | 1.000 | 1.000 | 0.750 |" in text
    assert "| hybrid | invertido | 1.000 | 1.000 | 0.500 |" in text
    assert "| hybrid | esperado | 1.000 | 1.000 | 0.625 |" in text
    assert "| hybrid | peor orden posible | — | — | 0.500 |" in text
    assert "| hybrid | mejor orden posible | — | — | 0.750 |" in text
    assert "(2 de 2): QA, QB." in text
    assert (
        "depende del desempate en QB; el rango recíproco o el recall@3 cambian en ninguna" in text
    )


def test_the_tie_break_reading_says_the_hybrid_advantage_is_partly_luck() -> None:
    text = with_tie_break()

    # bm25 has MRR 0.75 in this toy; the hybrid ranges from 0.5 to 0.75 over the three variants.
    assert "La ventaja del híbrido sobre BM25 es en parte suerte del desempate" in text
    assert "El rango de las tres variantes es [0.500, 0.750]" in text
    assert "En el peor orden posible el híbrido queda por debajo de BM25." in text


def test_the_reading_says_the_policy_effect_is_the_policys_when_it_removes_the_dependence() -> None:
    text = with_tie_break()

    assert "QA dependían del desempate en el híbrido y con `hybrid+meta(demote)` ya no" in text
    assert "El MRR de `hybrid+meta(demote)` (0.750) no depende del desempate." in text


def test_without_tie_break_rows_the_report_has_no_such_section() -> None:
    assert "desempate de RRF" not in markdown()


def test_the_json_keeps_the_tie_break_numbers_and_the_method() -> None:
    data = json.loads(to_json(replace(evaluation(), tie_break=TIE_BREAK), PROVENANCE, QUESTIONS))

    tie = data["tie_break"]
    assert tie["rrf_k"] == 60
    assert "no sampling" in tie["method"]
    first = tie["strategies"][0]
    assert first["default"] == {"recall_at_3": 1.0, "hit_at_3": 1.0, "mrr": 0.75}
    assert first["reversed"]["mrr"] == 0.5
    assert first["expected"]["mrr"] == 0.625
    assert (first["worst_case_mrr"], first["best_case_mrr"]) == (0.5, 0.75)
    assert first["questions_with_exact_tie"] == ["QA", "QB"]
    assert first["first_two_order_depends"] == ["QA", "QB"]
    assert first["outcome_depends"] == ["QA"]
