"""Cosine similarity and the dense index checked against values worked out by hand."""

import numpy as np
import pytest
from numpy.typing import NDArray

from merchant_assistant.knowledge.chunking import Chunk
from merchant_assistant.knowledge.errors import InvalidEmbeddingError
from merchant_assistant.knowledge.vector import VectorIndex, cosine_similarity
from merchant_assistant.shared.errors import DomainError


def vec(*values: float) -> NDArray[np.float64]:
    return np.array(values, dtype=np.float64)


def chunk(position: int) -> Chunk:
    return Chunk(f"D{position}#0", f"D{position}", 0, None, "t", "t")


def chunks(count: int) -> list[Chunk]:
    return [chunk(position) for position in range(count)]


# --- cosine similarity -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # orthogonal: 1*0 + 0*1 = 0
        (vec(1, 0), vec(0, 1), 0.0),
        # orthogonal in 3-D: 1*0 + 2*0 + 0*5 = 0
        (vec(1, 2, 0), vec(0, 0, 5), 0.0),
        # parallel: dot = 1*2 + 2*4 + 3*6 = 28; |a||b| = sqrt(14) * sqrt(56) = sqrt(784) = 28
        (vec(1, 2, 3), vec(2, 4, 6), 1.0),
        # opposite: dot = -1 - 4 = -5; |a||b| = sqrt(5) * sqrt(5) = 5
        (vec(1, 2), vec(-1, -2), -1.0),
        # 3-4-5: dot = 1*3 + 0*4 = 3; |a||b| = 1 * 5 = 5
        (vec(1, 0), vec(3, 4), 0.6),
        # dot = 3*4 + 4*3 = 24; |a||b| = 5 * 5 = 25
        (vec(3, 4), vec(4, 3), 0.96),
        # dot = 1*2 + 2*3 + 2*6 = 20; |a| = sqrt(9) = 3; |b| = sqrt(4 + 9 + 36) = 7
        (vec(1, 2, 2), vec(2, 3, 6), 20 / 21),
    ],
)
def test_cosine_similarity_matches_the_hand_computed_value(
    a: NDArray[np.float64], b: NDArray[np.float64], expected: float
) -> None:
    assert cosine_similarity(a, b) == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_cosine_similarity_does_not_depend_on_the_length_of_the_vectors() -> None:
    # (30, 40) . (0.4, 0.3) = 12 + 12 = 24; |a||b| = 50 * 0.5 = 25 -> 0.96, as for (3, 4), (4, 3)
    assert cosine_similarity(vec(30, 40), vec(0.4, 0.3)) == pytest.approx(0.96, rel=1e-9)


def test_cosine_similarity_is_symmetric() -> None:
    assert cosine_similarity(vec(1, 2, 2), vec(2, 3, 6)) == cosine_similarity(
        vec(2, 3, 6), vec(1, 2, 2)
    )


def test_rounding_never_pushes_the_result_outside_minus_one_and_one() -> None:
    value = cosine_similarity(vec(0.1, 0.2, 0.3), vec(0.1, 0.2, 0.3))

    assert -1.0 <= value <= 1.0


@pytest.mark.parametrize(
    ("a", "b", "fragment"),
    [
        (vec(1, 2), vec(1, 2, 3), "shape mismatch"),
        (np.ones((2, 2)), np.ones((2, 2)), "1-D"),
        (vec(1, 2), np.ones((1, 2)), "1-D"),
        (np.float64(1.0) * np.ones(()), vec(1), "1-D"),
        (vec(1, float("nan")), vec(1, 2), "finite"),
        (vec(1, 2), vec(float("inf"), 2), "finite"),
        (vec(0, 0), vec(1, 2), "zero vector"),
        (vec(1, 2), vec(0, 0), "zero vector"),
        (vec(), vec(), "zero vector"),
    ],
)
def test_cosine_similarity_refuses_malformed_input(
    a: NDArray[np.float64], b: NDArray[np.float64], fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        cosine_similarity(a, b)


# --- vector index ------------------------------------------------------------------------

# Four chunks in the plane, deliberately not unit length:
#   c0 = (1, 0)   c1 = (1, 1)   c2 = (0, 2)   c3 = (-1, 0)
TOY = np.array([[1, 0], [1, 1], [0, 2], [-1, 0]], dtype=np.float64)


def test_the_index_ranks_chunks_by_cosine_similarity_with_exact_scores() -> None:
    # query (1, 0), unit already:
    #   c0: 1;  c1: (1, 1)/sqrt2 . (1, 0) = 0.7071067812;  c2: (0, 1) . (1, 0) = 0;  c3: -1
    results = VectorIndex(chunks(4), TOY).search(vec(1, 0), k=4)

    assert [r.chunk_id for r in results] == ["D0#0", "D1#0", "D2#0", "D3#0"]
    assert [r.score for r in results] == [
        pytest.approx(1.0, rel=1e-9),
        pytest.approx(0.7071067812, rel=1e-9),
        pytest.approx(0.0, abs=1e-12),
        pytest.approx(-1.0, rel=1e-9),
    ]


def test_the_index_ranking_changes_with_the_direction_of_the_query() -> None:
    # query (3, 4), |q| = 5, unit (0.6, 0.8):
    #   c1: (1, 1)/sqrt2 . (0.6, 0.8) = 1.4 / sqrt2 = 7 * sqrt2 / 10 = 0.9899494937
    #   c2: (0, 1) . (0.6, 0.8) = 0.8
    #   c0: (1, 0) . (0.6, 0.8) = 0.6
    #   c3: (-1, 0) . (0.6, 0.8) = -0.6
    results = VectorIndex(chunks(4), TOY).search(vec(3, 4), k=4)

    assert [r.chunk_id for r in results] == ["D1#0", "D2#0", "D0#0", "D3#0"]
    assert [r.score for r in results] == [
        pytest.approx(0.9899494937, rel=1e-9),
        pytest.approx(0.8, rel=1e-9),
        pytest.approx(0.6, rel=1e-9),
        pytest.approx(-0.6, rel=1e-9),
    ]


def test_the_score_does_not_depend_on_the_length_of_the_query() -> None:
    index = VectorIndex(chunks(4), TOY)

    big = index.search(vec(300, 400), k=4)
    small = index.search(vec(0.03, 0.04), k=4)

    assert [r.chunk_id for r in big] == [r.chunk_id for r in small]
    assert [r.score for r in big] == pytest.approx([r.score for r in small], rel=1e-9)


def test_search_returns_k_results_when_the_corpus_is_larger() -> None:
    results = VectorIndex(chunks(4), TOY).search(vec(3, 4), k=2)

    assert [r.chunk_id for r in results] == ["D1#0", "D2#0"]


def test_search_returns_everything_when_k_exceeds_the_corpus() -> None:
    assert len(VectorIndex(chunks(4), TOY).search(vec(3, 4), k=10)) == 4


def test_search_returns_even_poor_matches_because_the_cutoff_is_not_its_concern() -> None:
    results = VectorIndex(chunks(1), np.array([[-1.0, 0.0]])).search(vec(1, 0), k=1)

    assert [r.score for r in results] == [pytest.approx(-1.0, rel=1e-9)]


def test_chunks_with_the_same_direction_keep_their_corpus_order() -> None:
    # c0 and c1 are identical rows, so they score the same; c2 is orthogonal to the query.
    matrix = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, -1.0]])

    results = VectorIndex(chunks(3), matrix).search(vec(1, 1), k=3)

    assert [r.chunk_id for r in results] == ["D0#0", "D1#0", "D2#0"]
    assert results[0].score == results[1].score


def test_the_doc_id_travels_with_each_result() -> None:
    first = Chunk("DOC-07#3", "DOC-07", 3, None, "t", "t")

    result = VectorIndex([first], np.array([[1.0, 0.0]])).search(vec(1, 0), k=1)[0]

    assert (result.chunk_id, result.doc_id) == ("DOC-07#3", "DOC-07")


def test_an_empty_corpus_searches_to_an_empty_result() -> None:
    assert VectorIndex([], np.zeros((0, 3))).search(vec(1, 0, 0), k=3) == []


def test_changing_the_input_matrix_afterwards_does_not_change_the_index() -> None:
    matrix = TOY.copy()
    index = VectorIndex(chunks(4), matrix)

    matrix[:] = 0.0

    assert index.search(vec(1, 0), k=1)[0].chunk_id == "D0#0"


@pytest.mark.parametrize(
    ("matrix", "fragment"),
    [
        (np.ones((3, 2)), "expected a"),
        (np.ones(2), "expected a"),
        (np.ones((2, 2, 2)), "expected a"),
        (np.array([[1.0, float("nan")], [1.0, 1.0]]), "NaN or infinite"),
        (np.array([[1.0, float("inf")], [1.0, 1.0]]), "NaN or infinite"),
        (np.array([[1.0, 1.0], [0.0, 0.0]]), "zero embedding for chunk D1#0"),
    ],
)
def test_an_unusable_matrix_is_refused_with_a_domain_error(
    matrix: NDArray[np.float64], fragment: str
) -> None:
    with pytest.raises(InvalidEmbeddingError) as refused:
        VectorIndex(chunks(2), matrix)

    assert fragment in refused.value.detail


def test_the_invalid_embedding_error_has_a_stable_code_and_a_safe_message() -> None:
    with pytest.raises(InvalidEmbeddingError) as refused:
        VectorIndex(chunks(1), np.zeros((1, 2)))

    assert isinstance(refused.value, DomainError)
    assert refused.value.public() == {
        "code": "invalid_embedding",
        "message": "The embeddings returned by the provider are not usable.",
    }


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        (vec(1, 0, 0), "expected a query of shape"),
        (np.ones((1, 2)), "expected a query of shape"),
        (vec(float("nan"), 1), "NaN or infinite"),
        (vec(0, 0), "zero vector"),
    ],
)
def test_an_unusable_query_is_refused_with_a_domain_error(
    query: NDArray[np.float64], fragment: str
) -> None:
    with pytest.raises(InvalidEmbeddingError) as refused:
        VectorIndex(chunks(4), TOY).search(query, k=1)

    assert fragment in refused.value.detail


@pytest.mark.parametrize("k", [0, -2])
def test_searching_for_fewer_than_one_result_is_refused(k: int) -> None:
    with pytest.raises(ValueError, match="k must be"):
        VectorIndex(chunks(4), TOY).search(vec(1, 0), k=k)
