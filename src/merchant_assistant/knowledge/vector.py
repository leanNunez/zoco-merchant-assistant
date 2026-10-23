"""Cosine similarity and a dense index, with numpy and nothing else.

Vectors come from an external provider, so every malformed one is refused loudly: a zero
or NaN vector has no direction, and ranking it would produce an arbitrary order that looks
like a result.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from merchant_assistant.knowledge.chunking import Chunk
from merchant_assistant.knowledge.errors import InvalidEmbeddingError
from merchant_assistant.knowledge.ranking import ScoredChunk

Vector = NDArray[np.float64]


def cosine_similarity(a: Vector, b: Vector) -> float:
    """`a . b / (|a| |b|)`, undefined (so an error) when either vector has no direction."""
    left = np.asarray(a, dtype=np.float64)
    right = np.asarray(b, dtype=np.float64)
    if left.ndim != 1 or right.ndim != 1:
        raise ValueError("cosine similarity takes 1-D vectors")
    if left.shape != right.shape:
        raise ValueError(f"shape mismatch: {left.shape} vs {right.shape}")
    if not (np.isfinite(left).all() and np.isfinite(right).all()):
        raise ValueError("vectors must be finite")
    norms = float(np.linalg.norm(left)) * float(np.linalg.norm(right))
    if norms == 0.0:
        raise ValueError("cosine similarity is undefined for a zero vector")
    # Rounding can push a parallel pair to 1.0000000000000002; the true value is bounded.
    return min(1.0, max(-1.0, float(np.dot(left, right)) / norms))


class VectorIndex:
    def __init__(self, chunks: Sequence[Chunk], vectors: Vector) -> None:
        matrix = np.array(vectors, dtype=np.float64)  # a copy: later edits cannot reach the index
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise InvalidEmbeddingError(
                f"expected a ({len(chunks)}, d) matrix, got shape {matrix.shape}"
            )
        if not np.isfinite(matrix).all():
            raise InvalidEmbeddingError("the embeddings contain NaN or infinite values")
        norms = np.linalg.norm(matrix, axis=1)
        zero_rows = np.flatnonzero(norms == 0.0)
        if zero_rows.size:
            raise InvalidEmbeddingError(f"zero embedding for chunk {chunks[zero_rows[0]].id}")
        self._chunks = tuple(chunks)
        # Normalizing once turns every search into a single matrix product.
        self._unit = matrix / norms[:, np.newaxis]
        self._unit.setflags(write=False)

    def search(self, query: Vector, k: int) -> list[ScoredChunk]:
        """The `min(k, n)` most similar chunks, with no minimum score.

        Deciding that nothing is similar enough is a policy of the retriever, not of the
        index.
        """
        if k < 1:
            raise ValueError("k must be >= 1")
        vector = np.asarray(query, dtype=np.float64)
        dimension = self._unit.shape[1]
        if vector.shape != (dimension,):
            raise InvalidEmbeddingError(
                f"expected a query of shape ({dimension},), got {vector.shape}"
            )
        if not np.isfinite(vector).all():
            raise InvalidEmbeddingError("the query embedding contains NaN or infinite values")
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise InvalidEmbeddingError("the query embedding is a zero vector")
        scores = self._unit @ (vector / norm)
        # A stable sort on the negated score keeps the corpus order among equal scores.
        order = np.argsort(-scores, kind="stable")[:k]
        return [
            ScoredChunk(self._chunks[i].id, self._chunks[i].doc_id, float(scores[i])) for i in order
        ]
