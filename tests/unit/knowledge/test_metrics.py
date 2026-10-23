from collections.abc import Callable

import pytest

from merchant_assistant.knowledge.metrics import hit_at_k, mean, recall_at_k, reciprocal_rank

Measure = Callable[[list[str], set[str], int], float]

RANKED = ["DOC-05", "DOC-12", "DOC-07", "DOC-01"]


@pytest.mark.parametrize(
    ("relevant", "k", "recall", "hit"),
    [
        # one relevant document
        ({"DOC-05"}, 1, 1.0, 1.0),  # first place
        ({"DOC-07"}, 2, 0.0, 0.0),  # third place is outside the top 2
        ({"DOC-07"}, 3, 1.0, 1.0),  # third place is inside the top 3
        ({"DOC-99"}, 4, 0.0, 0.0),  # not in the ranking at all
        # two relevant documents
        ({"DOC-05", "DOC-07"}, 2, 0.5, 1.0),  # 1 of 2 found: recall 0.5, but a hit
        ({"DOC-05", "DOC-07"}, 3, 1.0, 1.0),  # 2 of 2 found
        ({"DOC-07", "DOC-01"}, 2, 0.0, 0.0),  # neither is in the top 2
        ({"DOC-05", "DOC-99"}, 4, 0.5, 1.0),  # one is not even retrievable
        # three relevant documents, two found in the top 3
        ({"DOC-05", "DOC-12", "DOC-99"}, 3, 2 / 3, 1.0),
    ],
)
def test_recall_and_hit_over_literal_rankings(
    relevant: set[str], k: int, recall: float, hit: float
) -> None:
    assert recall_at_k(RANKED, relevant, k) == pytest.approx(recall, rel=1e-9)
    assert hit_at_k(RANKED, relevant, k) == hit


def test_a_question_with_two_relevant_documents_found_halfway_has_recall_half_and_hit_one() -> None:
    # Q01 in the kit: DOC-01 and DOC-02 are relevant; only DOC-01 is in the top 3.
    ranked = ["DOC-01", "DOC-09", "DOC-04", "DOC-02"]

    assert recall_at_k(ranked, {"DOC-01", "DOC-02"}, 3) == 0.5
    assert hit_at_k(ranked, {"DOC-01", "DOC-02"}, 3) == 1.0


def test_a_question_whose_relevant_documents_never_appear_scores_zero_everywhere() -> None:
    ranked = ["DOC-09", "DOC-04", "DOC-03"]
    relevant = {"DOC-01", "DOC-02"}

    assert recall_at_k(ranked, relevant, 3) == 0.0
    assert hit_at_k(ranked, relevant, 3) == 0.0
    assert reciprocal_rank(ranked, relevant) == 0.0


@pytest.mark.parametrize(
    ("relevant", "expected"),
    [
        ({"DOC-05"}, 1.0),  # rank 1
        ({"DOC-12"}, 0.5),  # rank 2
        ({"DOC-07"}, 1 / 3),  # rank 3
        ({"DOC-01"}, 0.25),  # rank 4
        ({"DOC-99"}, 0.0),  # absent
        ({"DOC-07", "DOC-12"}, 0.5),  # only the first relevant one counts: DOC-12 at rank 2
        ({"DOC-01", "DOC-05"}, 1.0),  # the best placed of the two, rank 1
    ],
)
def test_reciprocal_rank_is_one_over_the_rank_of_the_first_relevant_document(
    relevant: set[str], expected: float
) -> None:
    assert reciprocal_rank(RANKED, relevant) == pytest.approx(expected, rel=1e-9)


def test_k_larger_than_the_ranking_looks_at_the_whole_ranking() -> None:
    assert recall_at_k(["DOC-01"], {"DOC-01", "DOC-02"}, 10) == 0.5


def test_an_empty_ranking_scores_zero() -> None:
    assert recall_at_k([], {"DOC-01"}, 3) == 0.0
    assert hit_at_k([], {"DOC-01"}, 3) == 0.0
    assert reciprocal_rank([], {"DOC-01"}) == 0.0


def test_the_mean_is_the_sum_over_the_count() -> None:
    # (1 + 0.5 + 0) / 3 = 0.5
    assert mean([1.0, 0.5, 0.0]) == pytest.approx(0.5, rel=1e-9)
    # (1/3 + 1/3 + 1) / 3 = 5/9
    assert mean([1 / 3, 1 / 3, 1.0]) == pytest.approx(5 / 9, rel=1e-9)


def test_the_mean_of_nothing_is_refused_instead_of_reported_as_zero() -> None:
    with pytest.raises(ValueError, match="no values"):
        mean([])


@pytest.mark.parametrize("measure", [recall_at_k, hit_at_k])
def test_a_question_without_relevant_documents_cannot_be_graded(measure: Measure) -> None:
    with pytest.raises(ValueError, match="relevant must not be empty"):
        measure(RANKED, set(), 3)


def test_reciprocal_rank_of_a_question_without_relevant_documents_is_refused() -> None:
    with pytest.raises(ValueError, match="relevant must not be empty"):
        reciprocal_rank(RANKED, set())


@pytest.mark.parametrize("k", [0, -1])
@pytest.mark.parametrize("measure", [recall_at_k, hit_at_k])
def test_a_cutoff_below_one_is_refused(measure: Measure, k: int) -> None:
    with pytest.raises(ValueError, match="k must be"):
        measure(RANKED, {"DOC-05"}, k)
