from merchant_assistant.knowledge.ranking import (
    ScoredChunk,
    best_chunk_per_document,
    document_ranking,
)

SCORED = [
    ScoredChunk("DOC-05#1", "DOC-05", 0.9),
    ScoredChunk("DOC-07#0", "DOC-07", 0.8),
    ScoredChunk("DOC-05#0", "DOC-05", 0.7),
    ScoredChunk("DOC-12#2", "DOC-12", 0.6),
    ScoredChunk("DOC-07#2", "DOC-07", 0.5),
]


def test_each_document_keeps_only_its_best_chunk_in_the_original_order() -> None:
    assert best_chunk_per_document(SCORED) == [SCORED[0], SCORED[1], SCORED[3]]


def test_the_document_ranking_lists_each_document_once_by_its_best_chunk() -> None:
    assert document_ranking(SCORED) == ["DOC-05", "DOC-07", "DOC-12"]


def test_the_first_chunk_wins_even_if_the_caller_did_not_sort_by_score() -> None:
    unsorted = [ScoredChunk("A#0", "A", 0.1), ScoredChunk("A#1", "A", 0.9)]

    assert best_chunk_per_document(unsorted) == [unsorted[0]]


def test_a_ranking_without_chunks_is_empty() -> None:
    assert best_chunk_per_document([]) == []
    assert document_ranking([]) == []


def test_a_lower_scored_document_is_not_pushed_out_by_extra_chunks_of_a_better_one() -> None:
    scored = [ScoredChunk(f"A#{n}", "A", 1.0 - n / 10) for n in range(5)]
    scored.append(ScoredChunk("B#0", "B", 0.1))

    assert document_ranking(scored)[:2] == ["A", "B"]
