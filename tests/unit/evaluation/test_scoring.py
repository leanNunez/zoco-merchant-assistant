from collections.abc import Sequence

import pytest
from evaluation.retrieval.scoring import (
    Interval,
    QuestionOutcome,
    StrategyRun,
    bootstrap_interval,
    evaluate,
    paired_difference_interval,
    score_question,
)

from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.ranking import ScoredChunk

TWO_RELEVANT = Question("Q01", "¿?", frozenset({"DOC-01", "DOC-02"}))


def test_a_question_is_scored_on_its_document_ranking() -> None:
    outcome = score_question(["DOC-05", "DOC-02", "DOC-07", "DOC-01"], TWO_RELEVANT)

    # Only DOC-02 is in the top 3: recall 1/2, hit 1; first relevant at rank 2: RR 1/2.
    assert outcome == QuestionOutcome(
        question_id="Q01",
        ranking=("DOC-05", "DOC-02", "DOC-07", "DOC-01"),
        recall_at_3=0.5,
        hit_at_3=1.0,
        reciprocal_rank=0.5,
        first_relevant_rank=2,
    )


def test_a_question_whose_documents_never_appear_scores_zero_with_no_rank() -> None:
    outcome = score_question(["DOC-05"], TWO_RELEVANT)

    assert (outcome.recall_at_3, outcome.hit_at_3, outcome.reciprocal_rank) == (0.0, 0.0, 0.0)
    assert outcome.first_relevant_rank is None


def test_an_empty_ranking_is_a_miss_not_an_error() -> None:
    assert score_question([], TWO_RELEVANT).reciprocal_rank == 0.0


class FixedRetriever:
    name = "fixed"

    def __init__(self, chunk_ids: Sequence[str]) -> None:
        self.chunk_ids = chunk_ids
        self.asked: list[int] = []

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        self.asked.append(k)
        return [ScoredChunk(c, c.split("#")[0], 1.0) for c in self.chunk_ids][:k]


def test_evaluate_asks_for_every_chunk_and_ranks_documents_by_their_best_chunk() -> None:
    retriever = FixedRetriever(["DOC-05#0", "DOC-05#1", "DOC-01#3", "DOC-02#0"])
    question = Question("Q01", "¿?", frozenset({"DOC-02"}))

    run = evaluate(retriever, [question], chunking="section", chunks=4)

    assert retriever.asked == [4]
    assert run.outcomes[0].ranking == ("DOC-05", "DOC-01", "DOC-02")
    assert run.outcomes[0].first_relevant_rank == 3
    assert (run.strategy, run.chunking, run.chunks) == ("fixed", "section", 4)


def outcome(question_id: str, recall: float, hit: float, rr: float) -> QuestionOutcome:
    return QuestionOutcome(question_id, (), recall, hit, rr, None)


def test_a_run_averages_each_metric_over_its_questions() -> None:
    run = StrategyRun(
        "s",
        "c",
        1,
        (
            outcome("A", 1.0, 1.0, 1.0),
            outcome("B", 0.5, 1.0, 0.5),
            outcome("C", 0.0, 0.0, 0.0),
            outcome("D", 1.0, 1.0, 0.25),
        ),
    )

    # recall (1 + 0.5 + 0 + 1) / 4 = 0.625; hit 3/4; MRR (1 + 0.5 + 0 + 0.25) / 4 = 0.4375
    assert (run.recall_at_3, run.hit_at_3, run.mrr) == (0.625, 0.75, 0.4375)
    assert run.column("reciprocal_rank") == [1.0, 0.5, 0.0, 0.25]


def test_the_bootstrap_of_constant_values_is_that_constant() -> None:
    assert bootstrap_interval([0.5] * 20) == Interval(0.5, 0.5)


def test_the_bootstrap_interval_contains_the_mean_and_stays_within_the_values() -> None:
    values = [1.0] * 15 + [0.0] * 5  # mean 0.75

    interval = bootstrap_interval(values)

    assert 0.0 <= interval.low < 0.75 < interval.high <= 1.0


def test_the_bootstrap_is_reproducible_with_its_fixed_seed() -> None:
    values = [1.0, 0.5, 0.0, 0.25, 1.0, 1.0, 0.0, 0.5]

    assert bootstrap_interval(values) == bootstrap_interval(values)


def test_a_wider_spread_gives_a_wider_interval() -> None:
    narrow = bootstrap_interval([0.6, 0.4] * 10)
    wide = bootstrap_interval([1.0, 0.0] * 10)

    assert wide.high - wide.low > narrow.high - narrow.low


def test_the_bootstrap_refuses_no_values() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        bootstrap_interval([])


def test_a_paired_difference_is_the_mean_of_per_question_differences() -> None:
    # differences: 0.5, 0, 0.5, 0 -> mean 0.25
    mean_difference, interval = paired_difference_interval(
        [1.0, 1.0, 1.0, 0.5], [0.5, 1.0, 0.5, 0.5]
    )

    assert mean_difference == 0.25
    assert 0.0 <= interval.low <= 0.25 <= interval.high <= 0.5


def test_identical_strategies_differ_by_exactly_zero() -> None:
    values = [1.0, 0.5, 0.0, 1.0]

    assert paired_difference_interval(values, values) == (0.0, Interval(0.0, 0.0))


def test_a_paired_difference_needs_the_same_questions_on_both_sides() -> None:
    with pytest.raises(ValueError, match="same questions"):
        paired_difference_interval([1.0], [1.0, 0.0])
