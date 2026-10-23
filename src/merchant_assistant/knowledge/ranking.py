"""From scored chunks to a ranking of documents.

Retrieval scores chunks, but the questions are answered and graded per document: a
document is as good as its best chunk, and a second chunk of the same document must not
push another document out of the top 3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk_id: str
    doc_id: str
    score: float


def best_chunk_per_document(scored: Iterable[ScoredChunk]) -> list[ScoredChunk]:
    """Keep the first chunk of each document, in the order given.

    The caller passes the chunks already sorted from best to worst, so the first one seen
    is the best one. Re-sorting here would hide a caller that forgot to sort.
    """
    seen: set[str] = set()
    best: list[ScoredChunk] = []
    for chunk in scored:
        if chunk.doc_id not in seen:
            seen.add(chunk.doc_id)
            best.append(chunk)
    return best


def document_ranking(scored: Iterable[ScoredChunk]) -> list[str]:
    return [chunk.doc_id for chunk in best_chunk_per_document(scored)]
