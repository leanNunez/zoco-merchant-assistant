"""Retrieval metrics over a ranking of DOCUMENT ids.

A question may have more than one relevant document (`relevant` is a set), and the usual
single-answer definitions do not say what to do then. These are the definitions used:

- recall@k   = |top-k documents ∩ relevant| / |relevant|. With two relevant documents and
               one of them in the top k the recall is 0.5: the system found half of what
               the question needs. It is 1.0 only when every relevant document is found.
- hit@k      = 1.0 if ANY relevant document is in the top k, else 0.0. It answers "did the
               system surface something useful?" and is reported next to recall so that the
               two readings of a multi-document question stay visible.
- reciprocal rank = 1 / rank of the FIRST relevant document, 0.0 if none appears. MRR is
               the mean of it. Only the first relevant document counts, so a question with
               two relevant documents is not rewarded or punished for the second one.

The rankings are lists of distinct document ids (see `ranking.document_ranking`); chunks
never reach this module, so a document with several good chunks counts once.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    _validate(relevant, k)
    found = set(ranked[:k]) & set(relevant)
    return len(found) / len(set(relevant))


def hit_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    _validate(relevant, k)
    return 1.0 if set(ranked[:k]) & set(relevant) else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float:
    _validate(relevant, 1)
    targets = set(relevant)
    for rank, document_id in enumerate(ranked, start=1):
        if document_id in targets:
            return 1 / rank
    return 0.0


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("the mean of no values is undefined")
    return sum(values) / len(values)


def _validate(relevant: Collection[str], k: int) -> None:
    # Without a relevant document the metric would be 0/0, and returning 0.0 would quietly
    # drag an average down for a question that cannot be graded.
    if not relevant:
        raise ValueError("relevant must not be empty")
    if k < 1:
        raise ValueError("k must be >= 1")
