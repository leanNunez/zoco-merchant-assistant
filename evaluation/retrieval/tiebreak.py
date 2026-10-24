"""How much the hybrid result depends on the arbitrary RRF tie-break.

Two documents can get exactly the same fused score: one is first in the vector ranking and
second in the BM25 ranking, the other the reverse, so both add 1/61 + 1/62. The production
fusion (`knowledge/fusion.py`) breaks that tie by best rank and then by document id
(ascending). That is a convention, not evidence, and with 20 questions it can move the MRR.

This module recomputes the fusion with its own RRF, so the production function is not
touched, and measures the same strategy under three tie-breaks:

- `default`: the production convention (best rank, then id ascending);
- `reversed`: best rank, then id descending;
- `expected`: every exact-tie group ordered uniformly at random, computed EXACTLY by
  enumerating the permutations of each group (the groups are tiny), so there is no sampling
  and the result is byte-reproducible.

It also gives the exact worst and best MRR over all the orderings. Everything here is
informational: it never takes part in the pre-declared selection.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import permutations, product

from evaluation.retrieval.scoring import QuestionOutcome, score_question
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.ranking import ScoredChunk, document_ranking
from merchant_assistant.knowledge.retrievers import Retriever

# The constant of `HybridRetriever`. The check against the production run in
# `tie_break_sensitivity` fails loudly if the two ever differ.
RRF_K = 60
# Orderings enumerated per question. The real groups are pairs (a handful per question);
# a corpus with far more ties needs a sampling method, and this refuses to guess.
MAX_ORDERINGS = 200_000


@dataclass(frozen=True, slots=True)
class TieGroup:
    """Ids with exactly the same fused score, in the production order and in the reverse one."""

    score: float
    default: tuple[str, ...]
    reverse: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Metrics:
    recall_at_3: float
    hit_at_3: float
    mrr: float


@dataclass(frozen=True, slots=True)
class TieBreakRow:
    """One strategy under the three tie-breaks, plus the questions that depend on them."""

    strategy: str
    chunking: str
    default: Metrics
    reversed: Metrics
    expected: Metrics
    worst_mrr: float
    best_mrr: float
    with_ties: tuple[str, ...]
    first_two_depend: tuple[str, ...]
    outcome_depends: tuple[str, ...]


def fuse_with_ties(rankings: Sequence[Sequence[str]], *, k: int = RRF_K) -> tuple[TieGroup, ...]:
    """RRF as `knowledge/fusion.py` computes it, but keeping each exact-tie group whole.

    Groups come best score first. Inside a group, `default` is the production order
    (best rank, then id ascending) and `reverse` is best rank, then id descending.
    """
    contributions: dict[str, list[float]] = {}
    best_rank: dict[str, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), start=1):
            contributions.setdefault(item, []).append(1 / (k + rank))
            best_rank[item] = min(best_rank.get(item, rank), rank)
    by_score: dict[float, list[str]] = {}
    for item, parts in contributions.items():
        by_score.setdefault(math.fsum(parts), []).append(item)
    groups = []
    for score in sorted(by_score, reverse=True):
        ids = by_score[score]
        default = tuple(sorted(ids, key=lambda item: (best_rank[item], item)))
        reverse = tuple(sorted(sorted(ids, reverse=True), key=lambda item: best_rank[item]))
        groups.append(TieGroup(score, default, reverse))
    return tuple(groups)


def default_order(groups: Sequence[TieGroup]) -> list[str]:
    return [item for group in groups for item in group.default]


def reversed_order(groups: Sequence[TieGroup]) -> list[str]:
    return [item for group in groups for item in group.reverse]


def _movable(group: TieGroup, doc_of: Mapping[str, str]) -> bool:
    """A group matters only if its members belong to more than one document."""
    return len({doc_of[item] for item in group.default}) > 1


def ordering_count(groups: Sequence[TieGroup], doc_of: Mapping[str, str]) -> int:
    return math.prod(math.factorial(len(g.default)) for g in groups if _movable(g, doc_of))


def all_orders(groups: Sequence[TieGroup], doc_of: Mapping[str, str]) -> Iterator[list[str]]:
    """Every ordering of the ids, each exact-tie group permuted in every possible way.

    A group made of chunks of one single document is left as it is: its permutations cannot
    change the ranking of documents, and enumerating them would only multiply the work.
    """
    count = ordering_count(groups, doc_of)
    if count > MAX_ORDERINGS:
        raise ValueError(
            f"{count} tie orderings in one question: too many to enumerate exactly "
            f"(limit {MAX_ORDERINGS})"
        )
    options = [
        list(permutations(group.default)) if _movable(group, doc_of) else [group.default]
        for group in groups
    ]
    for combination in product(*options):
        yield [item for part in combination for item in part]


def _outcome(
    order: Sequence[str],
    question: Question,
    groups: Sequence[TieGroup],
    doc_of: Mapping[str, str],
    wrap: Callable[[Retriever], Retriever],
    strategy: str,
) -> QuestionOutcome:
    scores = {item: group.score for group in groups for item in group.default}
    ranking = [ScoredChunk(item, doc_of[item], scores[item]) for item in order]
    retriever = wrap(_Fixed(strategy, ranking))
    found = document_ranking(retriever.search(question.text, len(ranking)))
    return score_question(found, question)


class _Fixed:
    """A retriever that returns one given ranking: the fusion outcome under one tie-break."""

    def __init__(self, name: str, ranking: list[ScoredChunk]) -> None:
        self._name = name
        self._ranking = ranking

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        return self._ranking[:k]


def tie_break_sensitivity(
    *,
    strategy: str,
    chunking: str,
    questions: Sequence[Question],
    fused: Sequence[Sequence[TieGroup]],
    doc_of: Mapping[str, str],
    wrap: Callable[[Retriever], Retriever],
    reference: Sequence[QuestionOutcome],
) -> TieBreakRow:
    """Measure `strategy` under the three tie-breaks.

    `fused[i]` is the tie-grouped fusion of question `i`; `wrap` puts the strategy's policy
    (nothing for plain hybrid, the validity policy for `hybrid+meta(demote)`) on top of the
    fixed fusion order; `reference` is the production outcome the default tie-break must
    reproduce, so the ablation cannot drift from `fusion.py` unnoticed.
    """
    default_outcomes: list[QuestionOutcome] = []
    reversed_outcomes: list[QuestionOutcome] = []
    expected: list[tuple[float, float, float]] = []
    worst: list[float] = []
    best: list[float] = []
    with_ties: list[str] = []
    first_two: list[str] = []
    outcome_depends: list[str] = []
    for question, groups in zip(questions, fused, strict=True):
        outcome = partial(
            _outcome, question=question, groups=groups, doc_of=doc_of, wrap=wrap, strategy=strategy
        )
        default_outcomes.append(outcome(default_order(groups)))
        reversed_outcomes.append(outcome(reversed_order(groups)))
        every = [outcome(order) for order in all_orders(groups, doc_of)]
        expected.append(
            (
                math.fsum(o.recall_at_3 for o in every) / len(every),
                math.fsum(o.hit_at_3 for o in every) / len(every),
                math.fsum(o.reciprocal_rank for o in every) / len(every),
            )
        )
        worst.append(min(o.reciprocal_rank for o in every))
        best.append(max(o.reciprocal_rank for o in every))
        if any(_movable(group, doc_of) for group in groups):
            with_ties.append(question.id)
        if len({o.ranking[:2] for o in every}) > 1:
            first_two.append(question.id)
        if len({(o.reciprocal_rank, o.recall_at_3) for o in every}) > 1:
            outcome_depends.append(question.id)

    if [o.ranking for o in default_outcomes] != [o.ranking for o in reference]:
        raise ValueError(
            f"the default tie-break of {strategy} does not reproduce the production run"
        )
    return TieBreakRow(
        strategy=strategy,
        chunking=chunking,
        default=_metrics(default_outcomes),
        reversed=_metrics(reversed_outcomes),
        expected=Metrics(
            math.fsum(r for r, _, _ in expected) / len(expected),
            math.fsum(h for _, h, _ in expected) / len(expected),
            math.fsum(m for _, _, m in expected) / len(expected),
        ),
        worst_mrr=math.fsum(worst) / len(worst),
        best_mrr=math.fsum(best) / len(best),
        with_ties=tuple(with_ties),
        first_two_depend=tuple(first_two),
        outcome_depends=tuple(outcome_depends),
    )


def _metrics(outcomes: Sequence[QuestionOutcome]) -> Metrics:
    count = len(outcomes)
    return Metrics(
        math.fsum(o.recall_at_3 for o in outcomes) / count,
        math.fsum(o.hit_at_3 for o in outcomes) / count,
        math.fsum(o.reciprocal_rank for o in outcomes) / count,
    )
