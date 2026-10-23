"""Reciprocal Rank Fusion.

    RRF(d) = sum over the rankings r that contain d of 1 / (k + rank_r(d)),  rank 1-based

Ranks, not scores, are fused because BM25 scores and cosine similarities live on unrelated
scales; a rank is comparable across any retriever.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = 60
) -> list[tuple[str, float]]:
    """Fuse ranked lists of ids into one list of `(id, score)`, best first.

    An id repeated inside one ranking counts once, at its first position, and the ids
    after it move up: the rank is the position among distinct ids. Ties are broken by the
    best rank the id reached in any list and then by the id itself, so the output never
    depends on dictionary or set ordering.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    contributions: dict[str, list[float]] = {}
    best_rank: dict[str, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), start=1):
            contributions.setdefault(item, []).append(1 / (k + rank))
            best_rank[item] = min(best_rank.get(item, rank), rank)
    # `fsum` is exact, so ids with the same set of contributions get bit-identical scores
    # whatever the order of the lists, and a tie is a real tie.
    scores = {item: math.fsum(parts) for item, parts in contributions.items()}
    ordered = sorted(scores, key=lambda item: (-scores[item], best_rank[item], item))
    return [(item, scores[item]) for item in ordered]
