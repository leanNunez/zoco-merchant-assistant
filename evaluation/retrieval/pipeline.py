"""The whole retrieval evaluation as one pure function of the corpus and an embedder.

Nothing here reads files or knows the provider: the CLI loads the kit and passes the
cached embedder from the composition root, and the tests pass a hashing fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from evaluation.retrieval.scoring import Interval, StrategyRun, bootstrap_interval, evaluate
from evaluation.retrieval.scoring import paired_difference_interval as paired
from evaluation.retrieval.selection import ChunkingCandidate, Selection, pick_best, select_chunking
from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import Chunk, ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.retrievers import (
    BM25Retriever,
    HybridRetriever,
    KindDemotionRetriever,
    MetadataMode,
    MetadataRetriever,
    Retriever,
    VectorRetriever,
)
from merchant_assistant.knowledge.temporal import Period, reference_period
from merchant_assistant.knowledge.text import Tokenizer
from merchant_assistant.knowledge.vector import VectorIndex

# The evaluation must not depend on the day it runs: a fixed "today" keeps the results
# reproducible. 2026-10-23 is inside the validity of DOC-05 and DOC-07 (which expire on
# 2027-03-31) and after the end of the historical DOC-12 (2026-08-31).
REFERENCE_DATE = date(2026, 10, 23)

GRID: tuple[ChunkingStrategy, ...] = (
    ChunkingStrategy("document"),
    ChunkingStrategy("section"),
    ChunkingStrategy("section", header=True),
    ChunkingStrategy("window", size=32, overlap=0),
    ChunkingStrategy("window", size=32, overlap=5),
    ChunkingStrategy("window", size=64, overlap=0),
    ChunkingStrategy("window", size=64, overlap=10),
    ChunkingStrategy("window", size=128, overlap=0),
    ChunkingStrategy("window", size=128, overlap=19),
)

MAIN_STRATEGIES = ("vector", "bm25", "hybrid", "hybrid+meta(filter)", "hybrid+meta(demote)")
# Declared before running: on a full tie, the mode that never hides a document wins, and
# among single strategies BM25 wins because it needs no provider.
META_PREFERENCE = ("hybrid+meta(demote)", "hybrid+meta(filter)")
SINGLE_PREFERENCE = ("bm25", "vector")


@dataclass(frozen=True, slots=True)
class MainRow:
    run: StrategyRun
    recall_interval: Interval
    mrr_interval: Interval


@dataclass(frozen=True, slots=True)
class PairedComparison:
    label: str
    better: str
    baseline: str
    mean_difference: float
    interval: Interval


@dataclass(frozen=True, slots=True)
class RetrievalEvaluation:
    grid: tuple[StrategyRun, ...]
    selection: Selection
    main: tuple[MainRow, ...]
    paired: tuple[PairedComparison, ...]
    ablations: tuple[StrategyRun, ...]
    periods: dict[str, Period]
    chunk_counts: dict[str, int]


def with_header_toggled(strategy: ChunkingStrategy) -> ChunkingStrategy:
    return replace(strategy, header=not strategy.header)


def chunkings_to_index() -> tuple[ChunkingStrategy, ...]:
    """The grid plus each entry with its header toggled, so any header ablation is cached."""
    seen: dict[str, ChunkingStrategy] = {}
    for strategy in GRID:
        for variant in (strategy, with_header_toggled(strategy)):
            seen.setdefault(variant.label, variant)
    return tuple(seen.values())


def texts_to_index(documents: Sequence[Document], questions: Sequence[Question]) -> list[str]:
    """Every text the evaluation will embed: chunk index texts and question texts."""
    texts = [
        chunk.index_text
        for strategy in chunkings_to_index()
        for chunk in chunk_corpus(documents, strategy)
    ]
    texts.extend(question.text for question in questions)
    return texts


class _Strategies:
    """The retrievers of one chunking, built once."""

    def __init__(
        self, documents: Sequence[Document], strategy: ChunkingStrategy, embedder: Embedder
    ) -> None:
        self.label = strategy.label
        self.kind = strategy.kind
        self.chunks: tuple[Chunk, ...] = chunk_corpus(documents, strategy)
        vectors = embedder.embed([chunk.index_text for chunk in self.chunks])
        self.vector = VectorRetriever(VectorIndex(self.chunks, vectors), embedder)
        self.bm25 = BM25Retriever(BM25Index(self.chunks, Tokenizer(stem=False)))
        self.hybrid = HybridRetriever([self.vector, self.bm25], depth=len(self.chunks))
        self._documents = documents

    def bm25_stemmed(self) -> Retriever:
        return BM25Retriever(BM25Index(self.chunks, Tokenizer(stem=True)), name="bm25(stem)")

    def meta(self, mode: MetadataMode) -> Retriever:
        return MetadataRetriever(
            self.hybrid, self._documents, mode=mode, today=lambda: REFERENCE_DATE
        )

    def kind_demotion(self) -> Retriever:
        return KindDemotionRetriever(self.hybrid, self._documents, kind="historico")

    def run(self, retriever: Retriever, questions: Sequence[Question]) -> StrategyRun:
        return evaluate(retriever, questions, chunking=self.label, chunks=len(self.chunks))


def run_evaluation(
    documents: Sequence[Document], questions: Sequence[Question], embedder: Embedder
) -> RetrievalEvaluation:
    built = [_Strategies(documents, strategy, embedder) for strategy in GRID]
    grid: list[StrategyRun] = []
    candidates: list[ChunkingCandidate] = []
    for position, strategies in enumerate(built):
        runs = [
            strategies.run(retriever, questions)
            for retriever in (strategies.vector, strategies.bm25, strategies.hybrid)
        ]
        grid.extend(runs)
        hybrid = runs[2]
        candidates.append(
            ChunkingCandidate(
                strategies.label,
                strategies.kind,
                len(strategies.chunks),
                hybrid.mrr,
                hybrid.recall_at_3,
                position,
            )
        )

    selection = select_chunking(candidates)
    chosen = built[[c.label for c in candidates].index(selection.selected.label)]
    by_name = {run.strategy: run for run in grid if run.chunking == chosen.label}
    modes: tuple[MetadataMode, ...] = ("filter", "demote")
    for mode in modes:
        run = chosen.run(chosen.meta(mode), questions)
        by_name[run.strategy] = run
    main = tuple(
        MainRow(
            by_name[name],
            bootstrap_interval(by_name[name].column("recall_at_3")),
            bootstrap_interval(by_name[name].column("reciprocal_rank")),
        )
        for name in MAIN_STRATEGIES
    )

    best_meta = pick_best([by_name[name] for name in META_PREFERENCE], META_PREFERENCE)
    best_single = pick_best([by_name[name] for name in SINGLE_PREFERENCE], SINGLE_PREFERENCE)
    comparisons = tuple(
        _compare(label, better, baseline)
        for label, better, baseline in (
            ("mejor modo de metadata menos híbrido", best_meta, by_name["hybrid"]),
            ("híbrido menos la mejor estrategia simple", by_name["hybrid"], best_single),
        )
    )

    toggled = _Strategies(documents, with_header_toggled(GRID[_grid_index(chosen.label)]), embedder)
    ablations = (
        chosen.run(chosen.bm25_stemmed(), questions),
        *(
            toggled.run(retriever, questions)
            for retriever in (toggled.vector, toggled.bm25, toggled.hybrid)
        ),
        chosen.run(chosen.kind_demotion(), questions),
    )

    counts = {strategies.label: len(strategies.chunks) for strategies in built}
    counts[toggled.label] = len(toggled.chunks)
    return RetrievalEvaluation(
        grid=tuple(grid),
        selection=selection,
        main=main,
        paired=comparisons,
        ablations=ablations,
        periods={q.id: reference_period(q.text, REFERENCE_DATE) for q in questions},
        chunk_counts=counts,
    )


def _compare(label: str, better: StrategyRun, baseline: StrategyRun) -> PairedComparison:
    difference, interval = paired(
        better.column("reciprocal_rank"), baseline.column("reciprocal_rank")
    )
    return PairedComparison(label, better.strategy, baseline.strategy, difference, interval)


def _grid_index(label: str) -> int:
    return [strategy.label for strategy in GRID].index(label)
