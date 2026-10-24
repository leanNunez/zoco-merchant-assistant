"""The RRF tie-break ablation, on toy rankings whose reciprocal ranks are worked out by hand."""

import math
from collections.abc import Sequence

import pytest
from evaluation.retrieval.scoring import score_question
from evaluation.retrieval.tiebreak import (
    MAX_ORDERINGS,
    RRF_K,
    TieBreakRow,
    all_orders,
    default_order,
    fuse_with_ties,
    ordering_count,
    reversed_order,
    tie_break_sensitivity,
)
from hypothesis import given
from hypothesis import strategies as st

from merchant_assistant.knowledge.fusion import reciprocal_rank_fusion
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.ranking import ScoredChunk
from merchant_assistant.knowledge.retrievers import Retriever


def identity(base: Retriever) -> Retriever:
    return base


def measure(
    rankings: Sequence[Sequence[str]],
    relevant: set[str],
    doc_of: dict[str, str] | None = None,
) -> TieBreakRow:
    question = Question("Q", "q", frozenset(relevant))
    groups = fuse_with_ties(rankings)
    items = {item for ranking in rankings for item in ranking}
    docs = doc_of or {item: item for item in items}
    reference = score_question(
        [docs[item] for item, _ in reciprocal_rank_fusion(rankings)], question
    )
    return tie_break_sensitivity(
        strategy="toy",
        chunking="toy",
        questions=[question],
        fused=[groups],
        doc_of=docs,
        wrap=identity,
        reference=[reference],
    )


def test_two_documents_that_swap_places_tie_and_the_three_variants_read_off_by_hand() -> None:
    # k = 60. X is 1st in the first list and 2nd in the second; Y the reverse. Both add
    # 1/61 + 1/62, so they tie exactly; Z is 3rd in both lists: 2/63, below them.
    # Relevant: Y. Production order (best rank, then id ascending): X, Y, Z -> RR 1/2.
    # Reversed (id descending): Y, X, Z -> RR 1. Expected over X,Y and Y,X: (1/2 + 1) / 2 = 3/4.
    rankings = [["X", "Y", "Z"], ["Y", "X", "Z"]]

    row = measure(rankings, {"Y"})

    assert row.default.mrr == 0.5
    assert row.reversed.mrr == 1.0
    assert row.expected.mrr == 0.75
    assert (row.worst_mrr, row.best_mrr) == (0.5, 1.0)
    # The three documents are in the top 3 whatever the order.
    assert (row.default.recall_at_3, row.reversed.recall_at_3, row.expected.recall_at_3) == (
        1.0,
        1.0,
        1.0,
    )
    assert row.with_ties == ("Q",)
    assert row.first_two_depend == ("Q",)
    assert row.outcome_depends == ("Q",)


def test_a_group_of_three_is_enumerated_in_all_its_six_orders() -> None:
    # Three lists with one different document each: all of them are 1st in one list only,
    # 1/61 each, a three-way tie. Relevant: Z, which is 3rd (id ascending), 1st (descending),
    # and in each of the positions 1, 2, 3 twice over the 6 orders:
    # expected RR = (2 * 1 + 2 * 1/2 + 2 * 1/3) / 6 = 11/18.
    row = measure([["X"], ["Y"], ["Z"]], {"Z"})

    assert row.default.mrr == pytest.approx(1 / 3)
    assert row.reversed.mrr == 1.0
    assert row.expected.mrr == pytest.approx(11 / 18)
    assert (row.worst_mrr, row.best_mrr) == pytest.approx((1 / 3, 1.0))


def test_without_an_exact_tie_the_three_variants_agree() -> None:
    # X is 1st in both lists: 2/61, above Y (2/62). No tie at all.
    row = measure([["X", "Y"], ["X", "Y"]], {"Y"})

    assert row.default == row.reversed == row.expected
    assert row.default.mrr == 0.5
    assert row.with_ties == ()
    assert row.outcome_depends == ()


def test_a_policy_on_top_can_remove_the_dependence_on_the_tie_break() -> None:
    class DemoteX:
        """The validity policy in miniature: X goes after every other document."""

        def __init__(self, base: Retriever) -> None:
            self._base = base

        @property
        def name(self) -> str:
            return "demote-x"

        def search(self, query: str, k: int) -> list[ScoredChunk]:
            found = self._base.search(query, k)
            return [c for c in found if c.doc_id != "X"] + [c for c in found if c.doc_id == "X"]

    question = Question("Q", "q", frozenset({"Y"}))
    rankings = [["X", "Y", "Z"], ["Y", "X", "Z"]]
    reference = score_question(["Y", "Z", "X"], question)

    row = tie_break_sensitivity(
        strategy="toy+demote",
        chunking="toy",
        questions=[question],
        fused=[fuse_with_ties(rankings)],
        doc_of={"X": "X", "Y": "Y", "Z": "Z"},
        wrap=DemoteX,
        reference=[reference],
    )

    assert row.with_ties == ("Q",)  # the tie is still there ...
    assert row.outcome_depends == ()  # ... but the policy decides the question
    assert (row.default.mrr, row.reversed.mrr, row.expected.mrr) == (1.0, 1.0, 1.0)


def test_chunks_of_the_same_document_that_tie_are_not_a_tie_between_documents() -> None:
    # a1 and a2 are chunks of document A, b1 of document B. Each is 1st in its own list.
    doc_of = {"a1": "A", "a2": "A", "b1": "B"}

    assert ordering_count(fuse_with_ties([["a1"], ["b1"]]), doc_of) == 2  # A against B
    assert ordering_count(fuse_with_ties([["a1"], ["a2"]]), doc_of) == 1  # only A
    assert ordering_count(fuse_with_ties([["a1"], ["a2"], ["b1"]]), doc_of) == 6


def test_the_default_variant_is_the_production_tie_break_on_the_known_case() -> None:
    rankings = [["b", "a", "c"], ["a", "b", "d"]]

    groups = fuse_with_ties(rankings)

    assert default_order(groups) == [item for item, _ in reciprocal_rank_fusion(rankings)]
    assert default_order(groups) == ["a", "b", "c", "d"]  # a, b tie: id ascending; c, d too
    assert reversed_order(groups) == ["b", "a", "d", "c"]


@given(
    st.lists(
        st.lists(st.sampled_from("abcdefg"), max_size=7),  # repeated ids are allowed
        min_size=1,
        max_size=4,
    )
)
def test_the_default_variant_cannot_diverge_from_the_production_fusion(
    rankings: list[list[str]],
) -> None:
    production = reciprocal_rank_fusion(rankings)
    groups = fuse_with_ties(rankings)

    assert default_order(groups) == [item for item, _ in production]
    assert [g.score for g in groups for _ in g.default] == [score for _, score in production]


@given(
    st.lists(st.lists(st.sampled_from("abcdefg"), max_size=7), min_size=1, max_size=4),
    st.integers(min_value=1, max_value=100),
)
def test_the_fusion_keeps_the_same_ids_whatever_the_tie_break(
    rankings: list[list[str]], k: int
) -> None:
    groups = fuse_with_ties(rankings, k=k)

    assert sorted(default_order(groups)) == sorted(reversed_order(groups))
    assert [item for item, _ in reciprocal_rank_fusion(rankings, k=k)] == default_order(groups)


def test_the_enumeration_covers_every_order_once() -> None:
    groups = fuse_with_ties([["a", "b", "c"], ["b", "a", "d"]])  # {a, b} tie, {c, d} tie
    doc_of = {item: item for item in "abcd"}

    orders = [tuple(order) for order in all_orders(groups, doc_of)]

    assert len(orders) == len(set(orders)) == 4 == ordering_count(groups, doc_of)
    assert ("a", "b", "c", "d") in orders
    assert ("b", "a", "d", "c") in orders


def test_a_question_with_too_many_ties_is_refused_and_not_sampled() -> None:
    count = 1
    size = 1
    while count <= MAX_ORDERINGS:
        size += 1
        count = math.factorial(size)
    rankings = [[f"d{i}"] for i in range(size)]
    groups = fuse_with_ties(rankings)
    doc_of = {f"d{i}": f"d{i}" for i in range(size)}

    with pytest.raises(ValueError, match="too many to enumerate"):
        list(all_orders(groups, doc_of))


def test_a_default_that_does_not_reproduce_production_is_an_error() -> None:
    question = Question("Q", "q", frozenset({"X"}))
    wrong = score_question(["Y", "X"], question)  # production would put X first

    with pytest.raises(ValueError, match="does not reproduce"):
        tie_break_sensitivity(
            strategy="toy",
            chunking="toy",
            questions=[question],
            fused=[fuse_with_ties([["X", "Y"], ["X", "Y"]])],
            doc_of={"X": "X", "Y": "Y"},
            wrap=identity,
            reference=[wrong],
        )


def test_the_constant_is_the_one_of_the_hybrid_retriever() -> None:
    assert RRF_K == 60
