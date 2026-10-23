"""Reciprocal Rank Fusion checked against sums worked out by hand.

RRF(d) = sum over the lists that contain d of 1 / (k + rank), rank starting at 1.
"""

import pytest

from merchant_assistant.knowledge.fusion import reciprocal_rank_fusion


def assert_fused(actual: list[tuple[str, float]], expected: list[tuple[str, float]]) -> None:
    assert [item for item, _ in actual] == [item for item, _ in expected]
    assert [score for _, score in actual] == pytest.approx([s for _, s in expected], rel=1e-9)


def test_a_document_in_both_lists_adds_its_two_contributions() -> None:
    # k = 60. "a": rank 1 in the first list and rank 2 in the second.
    #   1/61 + 1/62 = 0.016393442623 + 0.016129032258 = 0.032522474881
    # "b": rank 2 and rank 1 -> the same two terms.
    # "c": rank 3 in one list only: 1/63 = 0.015873015873; "d": rank 3 in the other list only.
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])

    assert_fused(
        fused,
        [
            ("a", 0.032522474881),
            ("b", 0.032522474881),
            ("c", 0.015873015873),
            ("d", 0.015873015873),
        ],
    )


def test_a_document_missing_from_a_list_adds_nothing_for_that_list() -> None:
    # k = 60. "y": rank 2 in the first list, rank 1 in the second: 1/62 + 1/61 = 0.032522474881
    # "x": rank 1 in the first list only: 1/61 = 0.016393442623
    fused = reciprocal_rank_fusion([["x", "y"], ["y"]])

    assert_fused(
        fused,
        [
            ("y", 0.032522474881),
            ("x", 0.016393442623),
        ],
    )


def test_a_single_list_keeps_its_order_with_decreasing_scores() -> None:
    # k = 60: ranks 1, 2, 3 -> 1/61 = 0.016393442623, 1/62 = 0.016129032258, 1/63 = 0.015873015873
    fused = reciprocal_rank_fusion([["p", "q", "r"]])

    assert_fused(
        fused,
        [
            ("p", 0.016393442623),
            ("q", 0.016129032258),
            ("r", 0.015873015873),
        ],
    )


def test_with_k_equal_to_one_the_arithmetic_can_be_checked_by_eye() -> None:
    # k = 1: ranks 1, 2, 3 contribute 1/2, 1/3, 1/4.
    # "a": rank 1 in the first list, rank 3 in the second: 1/2 + 1/4 = 3/4
    # "b": rank 2 and rank 2: 1/3 + 1/3 = 2/3
    # "c": rank 3 in the first list only: 1/4;  "d": rank 1 in the second list only: 1/2
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["d", "b", "a"]], k=1)

    assert_fused(
        fused,
        [
            ("a", 0.75),
            ("b", 2 / 3),
            ("d", 0.5),
            ("c", 0.25),
        ],
    )


def test_a_smaller_k_gives_more_weight_to_the_top_of_each_list() -> None:
    # k = 1: top of a list 1/2 versus its second place 1/3 (ratio 1.5).
    # k = 60: 1/61 versus 1/62 (ratio 1.016): nearly flat.
    steep = dict(reciprocal_rank_fusion([["a", "b"]], k=1))
    flat = dict(reciprocal_rank_fusion([["a", "b"]], k=60))

    assert steep["a"] / steep["b"] == pytest.approx(1.5, rel=1e-9)
    assert flat["a"] / flat["b"] == pytest.approx(62 / 61, rel=1e-9)


def test_the_default_k_is_sixty() -> None:
    assert reciprocal_rank_fusion([["a"]]) == reciprocal_rank_fusion([["a"]], k=60)
    assert reciprocal_rank_fusion([["a"]])[0][1] == pytest.approx(1 / 61, rel=1e-9)


def test_an_id_repeated_inside_one_list_counts_once_at_its_first_position() -> None:
    # k = 60. ["a", "a", "b"] has the distinct ids a (rank 1) and b (rank 2):
    #   "a": 1/61 = 0.016393442623 (the repeat adds nothing);  "b": 1/62 = 0.016129032258
    fused = reciprocal_rank_fusion([["a", "a", "b"]])

    assert_fused(
        fused,
        [
            ("a", 0.016393442623),
            ("b", 0.016129032258),
        ],
    )


def test_ties_are_broken_by_best_rank_and_then_by_id() -> None:
    # k = 1, lists [z, m, a] and [n, o, a]:
    #   z: rank 1 -> 1/2;  n: rank 1 -> 1/2;  a: ranks 3 and 3 -> 1/4 + 1/4 = 1/2
    #   m: rank 2 -> 1/3;  o: rank 2 -> 1/3
    # Score ties: {n, z, a} at 1/2 and {m, o} at 1/3 (all values exact in binary or equal sums).
    # Among 1/2: z and n reached rank 1, a only rank 3 -> n and z first, by id (n < z), then a.
    # Among 1/3: both best rank 2 -> by id: m, o.
    fused = reciprocal_rank_fusion([["z", "m", "a"], ["n", "o", "a"]], k=1)

    assert [item for item, _ in fused] == ["n", "z", "a", "m", "o"]
    assert [score for _, score in fused[:3]] == [0.5, 0.5, 0.5]


def test_the_order_of_the_lists_does_not_change_the_fused_ranking() -> None:
    first = [["a", "b", "c"], ["c", "a", "d"], ["d", "c", "b"]]

    assert reciprocal_rank_fusion(first) == reciprocal_rank_fusion(first[::-1])


def test_no_rankings_fuse_to_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


@pytest.mark.parametrize("k", [0, -1, 1.5, True, "60"])
def test_k_must_be_a_positive_integer(k: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        reciprocal_rank_fusion([["a"]], k=k)  # type: ignore[arg-type]
