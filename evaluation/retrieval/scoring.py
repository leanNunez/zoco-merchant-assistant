"""Per-question metrics, their means and bootstrap intervals.

The ranking unit is the DOCUMENT: chunk results become a document ranking (each document
counts once, at its best chunk), and the retriever is asked for every chunk so the MRR
sees the whole list and not only a truncated top.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from merchant_assistant.knowledge.metrics import hit_at_k, mean, recall_at_k, reciprocal_rank
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.ranking import document_ranking
from merchant_assistant.knowledge.retrievers import Retriever

TOP_K = 3
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0


@dataclass(frozen=True, slots=True)
class QuestionOutcome:
    question_id: str
    ranking: tuple[str, ...]
    recall_at_3: float
    hit_at_3: float
    reciprocal_rank: float
    first_relevant_rank: int | None


@dataclass(frozen=True, slots=True)
class StrategyRun:
    strategy: str
    chunking: str
    chunks: int
    outcomes: tuple[QuestionOutcome, ...]

    @property
    def recall_at_3(self) -> float:
        return mean([outcome.recall_at_3 for outcome in self.outcomes])

    @property
    def hit_at_3(self) -> float:
        return mean([outcome.hit_at_3 for outcome in self.outcomes])

    @property
    def mrr(self) -> float:
        return mean([outcome.reciprocal_rank for outcome in self.outcomes])

    def column(self, metric: str) -> list[float]:
        return [float(getattr(outcome, metric)) for outcome in self.outcomes]


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


def score_question(ranking: Sequence[str], question: Question) -> QuestionOutcome:
    first = next(
        (rank for rank, doc_id in enumerate(ranking, start=1) if doc_id in question.relevant),
        None,
    )
    return QuestionOutcome(
        question_id=question.id,
        ranking=tuple(ranking),
        recall_at_3=recall_at_k(ranking, question.relevant, TOP_K),
        hit_at_3=hit_at_k(ranking, question.relevant, TOP_K),
        reciprocal_rank=reciprocal_rank(ranking, question.relevant),
        first_relevant_rank=first,
    )


def evaluate(
    retriever: Retriever,
    questions: Sequence[Question],
    *,
    chunking: str,
    chunks: int,
) -> StrategyRun:
    """Run every question through the retriever, asking for all `chunks` chunks."""
    outcomes = tuple(
        score_question(document_ranking(retriever.search(question.text, chunks)), question)
        for question in questions
    )
    return StrategyRun(retriever.name, chunking, chunks, outcomes)


def bootstrap_interval(
    values: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> Interval:
    """Percentile bootstrap of the mean, resampling questions with replacement.

    Every call starts from the same seed, so all strategies are resampled with the same
    question draws and their intervals are comparable.
    """
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 1 or data.size == 0:
        raise ValueError("the bootstrap needs a non-empty 1-D list of values")
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, data.size, size=(resamples, data.size))
    means = data[draws].mean(axis=1)
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(means, [tail, 100 - tail])
    return Interval(float(low), float(high))


def paired_difference_interval(
    a: Sequence[float],
    b: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, Interval]:
    """Mean of `a - b` per question and its bootstrap interval (same questions drawn)."""
    if len(a) != len(b):
        raise ValueError("a paired comparison needs the same questions on both sides")
    differences = [left - right for left, right in zip(a, b, strict=True)]
    return mean(differences), bootstrap_interval(differences, resamples=resamples, seed=seed)
