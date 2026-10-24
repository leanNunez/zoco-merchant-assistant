"""The whole retrieval evaluation as one pure function of the corpus and an embedder.

Nothing here reads files or knows the provider: the CLI loads the kit and passes the
cached embedder from the composition root, and the tests pass a hashing fake.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date

from evaluation.retrieval.scoring import Interval, StrategyRun, bootstrap_interval, evaluate
from evaluation.retrieval.scoring import paired_difference_interval as paired
from evaluation.retrieval.selection import ChunkingCandidate, Selection, pick_best, select_chunking
from evaluation.retrieval.tiebreak import TieBreakRow, fuse_with_ties, tie_break_sensitivity
from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import Chunk, ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.knowledge.questions import Question
from merchant_assistant.knowledge.retrievers import (
    BM25Retriever,
    FusionUnit,
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
class AblationDelta:
    """What an informational variant moved, question by question, against its reference.

    A question "improves" when its (reciprocal rank, recall@3) pair is higher, and "worsens"
    when it is lower. `reference` names the strategy compared against, on `reference_chunking`.
    """

    chunking: str
    strategy: str
    reference_chunking: str
    reference: str
    recall_delta: float
    hit_delta: float
    mrr_delta: float
    improved: tuple[str, ...]
    worsened: tuple[str, ...]

    @property
    def moved(self) -> int:
        return len(self.improved) + len(self.worsened)


@dataclass(frozen=True, slots=True)
class RetrievalEvaluation:
    grid: tuple[StrategyRun, ...]
    selection: Selection
    main: tuple[MainRow, ...]
    paired: tuple[PairedComparison, ...]
    ablations: tuple[StrategyRun, ...]
    periods: dict[str, Period]
    chunk_counts: dict[str, int]
    ablation_deltas: tuple[AblationDelta, ...] = ()
    tie_break: tuple[TieBreakRow, ...] = ()


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

    def bm25_variant(self, tokenizer: Tokenizer, name: str) -> Retriever:
        return BM25Retriever(BM25Index(self.chunks, tokenizer), name=name)

    def bm25_stemmed(self) -> Retriever:
        return self.bm25_variant(Tokenizer(stem=True), "bm25(stem)")

    def hybrid_of(
        self, lexical: Retriever, name: str, *, fuse_by: FusionUnit = "chunk"
    ) -> Retriever:
        return HybridRetriever(
            [self.vector, lexical], depth=len(self.chunks), name=name, fuse_by=fuse_by
        )

    def meta(self, mode: MetadataMode, base: Retriever | None = None) -> Retriever:
        return MetadataRetriever(
            self.hybrid if base is None else base,
            self._documents,
            mode=mode,
            today=lambda: REFERENCE_DATE,
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
    classic = [
        (chosen.run(chosen.bm25_stemmed(), questions), by_name["bm25"]),
        *((toggled.run(r, questions), by_name[r.name]) for r in (toggled.vector, toggled.bm25)),
        (toggled.run(toggled.hybrid, questions), by_name["hybrid"]),
        (chosen.run(chosen.kind_demotion(), questions), by_name["hybrid"]),
    ]
    pairs = [*classic, *_variants(chosen, built, questions, by_name, grid)]

    counts = {strategies.label: len(strategies.chunks) for strategies in built}
    counts[toggled.label] = len(toggled.chunks)
    return RetrievalEvaluation(
        grid=tuple(grid),
        selection=selection,
        main=main,
        paired=comparisons,
        ablations=tuple(run for run, _ in pairs),
        periods={q.id: reference_period(q.text, REFERENCE_DATE) for q in questions},
        chunk_counts=counts,
        ablation_deltas=tuple(ablation_delta(run, reference) for run, reference in pairs),
        tie_break=_tie_break(chosen, questions, by_name),
    )


def _tie_break(
    chosen: _Strategies, questions: Sequence[Question], by_name: dict[str, StrategyRun]
) -> tuple[TieBreakRow, ...]:
    """The hybrid and the hybrid with the validity policy under other RRF tie-breaks.

    The fusion is recomputed from the same two chunk rankings the hybrid retriever fuses, so
    the default tie-break must reproduce the main table's rows exactly (it is checked).
    """
    depth = len(chosen.chunks)
    doc_of = {chunk.id: chunk.doc_id for chunk in chosen.chunks}
    fused = [
        fuse_with_ties(
            [
                [found.chunk_id for found in retriever.search(question.text, depth)]
                for retriever in (chosen.vector, chosen.bm25)
            ]
        )
        for question in questions
    ]
    policies: tuple[tuple[str, Callable[[Retriever], Retriever]], ...] = (
        ("hybrid", lambda base: base),
        ("hybrid+meta(demote)", lambda base: chosen.meta("demote", base)),
    )
    return tuple(
        tie_break_sensitivity(
            strategy=name,
            chunking=chosen.label,
            questions=questions,
            fused=fused,
            doc_of=doc_of,
            wrap=wrap,
            reference=by_name[name].outcomes,
        )
        for name, wrap in policies
    )


def _variants(
    chosen: _Strategies,
    built: Sequence[_Strategies],
    questions: Sequence[Question],
    by_name: dict[str, StrategyRun],
    grid: Sequence[StrategyRun],
) -> list[tuple[StrategyRun, StrategyRun]]:
    """The informational tokenizer and fusion variants, each with the run it varies.

    They never take part in the pre-declared selection. The fusion by document is also run on
    the `section` chunking when that is not the selected one, because with one chunk per
    document (the `document` chunking) the fusion unit cannot matter by construction.
    """

    def named(run: StrategyRun, name: str) -> StrategyRun:
        return replace(run, strategy=name)

    stop = Tokenizer(stopwords=True)
    stop_bm25 = chosen.bm25_variant(stop, "bm25(stopwords)")
    stop_hybrid = chosen.hybrid_of(stop_bm25, "hybrid(stopwords)")
    by_document = chosen.hybrid_of(chosen.bm25, "hybrid(fuse by document)", fuse_by="document")
    pairs = [
        (chosen.run(chosen.bm25_variant(Tokenizer(emit_parts=False), "bm25(no parts)"), questions),
         by_name["bm25"]),
        (chosen.run(stop_bm25, questions), by_name["bm25"]),
        (chosen.run(chosen.bm25_variant(Tokenizer(stopwords=True, stem=True),
                                        "bm25(stopwords, stem)"), questions), by_name["bm25"]),
        (chosen.run(stop_hybrid, questions), by_name["hybrid"]),
        (named(chosen.run(chosen.meta("demote", stop_hybrid), questions),
               "hybrid+meta(demote)(stopwords)"), by_name["hybrid+meta(demote)"]),
        (chosen.run(by_document, questions), by_name["hybrid"]),
        (named(chosen.run(chosen.meta("demote", by_document), questions),
               "hybrid+meta(demote)(fuse by document)"), by_name["hybrid+meta(demote)"]),
    ]  # fmt: skip
    if chosen.label != "section":
        sections = next(strategies for strategies in built if strategies.label == "section")
        reference = next(r for r in grid if (r.chunking, r.strategy) == ("section", "hybrid"))
        fused = sections.hybrid_of(sections.bm25, "hybrid(fuse by document)", fuse_by="document")
        pairs.append((sections.run(fused, questions), reference))
    return pairs


def ablation_delta(run: StrategyRun, reference: StrategyRun) -> AblationDelta:
    improved: list[str] = []
    worsened: list[str] = []
    for outcome, base in zip(run.outcomes, reference.outcomes, strict=True):
        now = (outcome.reciprocal_rank, outcome.recall_at_3)
        before = (base.reciprocal_rank, base.recall_at_3)
        if now > before:
            improved.append(outcome.question_id)
        elif now < before:
            worsened.append(outcome.question_id)
    return AblationDelta(
        chunking=run.chunking,
        strategy=run.strategy,
        reference_chunking=reference.chunking,
        reference=reference.strategy,
        recall_delta=run.recall_at_3 - reference.recall_at_3,
        hit_delta=run.hit_at_3 - reference.hit_at_3,
        mrr_delta=run.mrr - reference.mrr,
        improved=tuple(improved),
        worsened=tuple(worsened),
    )


def _compare(label: str, better: StrategyRun, baseline: StrategyRun) -> PairedComparison:
    difference, interval = paired(
        better.column("reciprocal_rank"), baseline.column("reciprocal_rank")
    )
    return PairedComparison(label, better.strategy, baseline.strategy, difference, interval)


def _grid_index(label: str) -> int:
    return [strategy.label for strategy in GRID].index(label)
