import pytest
from evaluation.retrieval.scoring import QuestionOutcome, StrategyRun
from evaluation.retrieval.selection import ChunkingCandidate, pick_best, select_chunking

from merchant_assistant.knowledge.chunking import ChunkKind


def candidate(
    label: str,
    mrr: float,
    recall: float = 0.9,
    chunks: int = 10,
    kind: ChunkKind = "section",
    position: int = 0,
) -> ChunkingCandidate:
    return ChunkingCandidate(label, kind, chunks, mrr, recall, position)


def test_the_highest_hybrid_mrr_wins() -> None:
    selection = select_chunking([candidate("a", 0.80), candidate("b", 0.85, recall=0.5)])

    assert selection.selected.label == "b"
    assert selection.reason == (
        "`b` frente a `a` (el segundo): tiene el MRR híbrido más alto (0.850 contra 0.800)."
    )


def test_an_mrr_tie_is_broken_by_recall() -> None:
    selection = select_chunking([candidate("a", 0.8, recall=0.9), candidate("b", 0.8, recall=0.95)])

    assert selection.selected.label == "b"
    assert selection.reason == (
        "`b` frente a `a` (el segundo): empatan en MRR híbrido (0.800) y gana por recall@3 "
        "híbrido (0.950 contra 0.900)."
    )


def test_a_tie_that_differs_only_in_the_last_bit_is_still_a_tie() -> None:
    # 0.1 + 0.2 = 0.30000000000000004: the same mean computed in another order.
    selection = select_chunking([candidate("a", 0.1 + 0.2, chunks=20), candidate("b", 0.3)])

    assert selection.selected.label == "b"  # decided by fewer chunks, not by float noise


def test_then_fewer_chunks_win() -> None:
    selection = select_chunking([candidate("a", 0.8, chunks=49), candidate("b", 0.8, chunks=12)])

    assert selection.selected.label == "b"
    assert selection.reason.endswith("gana por tener menos chunks (12 contra 49).")


def test_then_the_simpler_kind_wins() -> None:
    selection = select_chunking(
        [
            candidate("w", 0.8, chunks=14, kind="window"),
            candidate("d", 0.8, chunks=14, kind="document"),
        ]
    )

    assert selection.selected.label == "d"
    assert selection.reason.endswith("gana por ser un tipo más simple (document antes que window).")


def test_finally_the_grid_order_wins() -> None:
    selection = select_chunking(
        [candidate("section+header", 0.8, position=2), candidate("section", 0.8, position=1)]
    )

    assert selection.selected.label == "section"
    assert selection.reason.endswith("gana por el orden de la grilla.")


def test_a_single_candidate_is_selected_and_says_so() -> None:
    assert select_chunking([candidate("a", 0.1)]).reason == "`a` es el único chunking evaluado."


def test_there_must_be_something_to_select() -> None:
    with pytest.raises(ValueError, match="no chunking"):
        select_chunking([])


def run(name: str, rr: float, recall: float) -> StrategyRun:
    return StrategyRun(name, "c", 1, (QuestionOutcome("Q", (), recall, 1.0, rr, 1),))


def test_pick_best_prefers_mrr_then_recall_then_the_declared_order() -> None:
    preference = ("hybrid+meta(demote)", "hybrid+meta(filter)")

    assert (
        pick_best(
            [run("hybrid+meta(filter)", 1.0, 1.0), run("hybrid+meta(demote)", 0.5, 1.0)], preference
        ).strategy
        == "hybrid+meta(filter)"
    )
    assert (
        pick_best(
            [run("hybrid+meta(filter)", 1.0, 1.0), run("hybrid+meta(demote)", 1.0, 0.5)], preference
        ).strategy
        == "hybrid+meta(filter)"
    )
    assert (
        pick_best(
            [run("hybrid+meta(filter)", 1.0, 1.0), run("hybrid+meta(demote)", 1.0, 1.0)], preference
        ).strategy
        == "hybrid+meta(demote)"
    )


def test_pick_best_refuses_a_strategy_without_a_declared_preference() -> None:
    with pytest.raises(ValueError, match="no declared preference"):
        pick_best([run("other", 1.0, 1.0)], ("bm25", "vector"))
