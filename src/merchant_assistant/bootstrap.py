"""Composition root: the only place that wires adapters to ports. The evaluation reuses it.

The agent reaches the capabilities only through its own ports (`agent/ports.py`); the thin
adapter classes here translate between the two vocabularies, so neither side imports the
other and import-linter can keep it that way.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import openai

from merchant_assistant.agent.application.conversation import ConversationService
from merchant_assistant.agent.application.executor import Sleeper, ToolExecutor
from merchant_assistant.agent.application.operator import OperatorService
from merchant_assistant.agent.ports import (
    ChatModel,
    ConversationStore,
    DayFigures,
    Passage,
    Registration,
    SalesOutcome,
    SearchOutcome,
)
from merchant_assistant.complaints.adapters.faulty_repository import FaultyComplaintRepository
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.models import ComplaintDraft
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.control import BusinessHours
from merchant_assistant.knowledge.adapters.filesystem import load_documents
from merchant_assistant.knowledge.adapters.missing_key import MissingKeyEmbedder
from merchant_assistant.knowledge.adapters.openai_embedder import OpenAIEmbedder
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import Chunk, ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.embedding import CachedEmbedder, embedding_key
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.knowledge.retrievers import (
    BM25Retriever,
    HybridRetriever,
    MetadataRetriever,
    Retriever,
    VectorRetriever,
)
from merchant_assistant.knowledge.text import Tokenizer
from merchant_assistant.knowledge.vector import VectorIndex
from merchant_assistant.sales.adapters.faulty_repository import FaultySalesRepository
from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.service import SalesService
from merchant_assistant.shared.clock import Clock, business_today
from merchant_assistant.shared.config import Settings
from merchant_assistant.shared.faults import FaultInjector

DOCS_DIR = Path("docs")  # the default of Settings.docs_dir; tests read the real corpus here
# Provisional until the measured selection of `make eval-retrieval` replaces it: sections are
# the corpus' own units, and validity is applied by demotion, which never hides a document
# (the tie preference the evaluation declares before measuring).
AGENT_CHUNKING = ChunkingStrategy("section")
PASSAGES_PER_SEARCH = 4
# The lexical floor of `buscar_documentacion` (SPEC_REVIEW F9, ADR-0004). BM25 scores any shared
# token (there are no stopwords) and a vector search always returns neighbours, so "no results"
# almost never happens by itself. Calibrated offline on this corpus and AGENT_CHUNKING, with
# no API: the lowest best score of the 20 kit questions is 5.871 (Q13) and the highest of 16
# off-topic questions is 4.626 ("¿Cómo se hace un asado?"); the floor sits at the midpoint,
# 0.62 from each side. tests/integration/agent/test_agent_retrieval_floor.py pins it.
MIN_BM25_SCORE = 5.25

SearchMode = Literal["hybrid", "degraded"]


def build_business_hours(settings: Settings) -> BusinessHours:
    """The human attention window of the settings, judged in the business timezone.

    `shared` may not import `control`, so the settings carry plain values and this is where
    they become the control machine's guard.
    """
    return BusinessHours(
        tz=settings.timezone,
        opens=settings.human_hours_start,
        closes=settings.human_hours_end,
        weekdays=settings.human_weekdays,
    )


def build_embedder(settings: Settings) -> CachedEmbedder:
    """Embeddings through the on-disk cache; without a key, only cached texts work."""
    inner: Embedder
    if settings.openai_api_key is None:
        inner = MissingKeyEmbedder(settings.embeddings_model)
    else:
        client = openai.OpenAI(
            api_key=settings.openai_api_key.get_secret_value(), timeout=30, max_retries=2
        )
        inner = OpenAIEmbedder(client, settings.embeddings_model)
    return CachedEmbedder(inner, SqliteEmbeddingCache(settings.embeddings_cache_path))


# ---------------------------------------------------------------------- capability adapters


class SearchAdapter:
    """A knowledge `Retriever` as the agent's `KnowledgeSearch`: passages with their validity.

    A query whose best BM25 score over the corpus is below `min_score` is `no_data`, whatever
    the retriever would return: that is the deterministic signal the turn turns into
    `low_confidence`, and it holds in hybrid mode too, where the vector side alone would always
    find neighbours.
    """

    def __init__(
        self,
        retriever: Retriever,
        chunks: tuple[Chunk, ...],
        documents: tuple[Document, ...],
        *,
        lexical: BM25Index,
        min_score: float = MIN_BM25_SCORE,
        k: int = PASSAGES_PER_SEARCH,
    ) -> None:
        if not min_score >= 0:  # written so that NaN is refused too
            raise ValueError("min_score must be >= 0")
        self._retriever = retriever
        self._chunks = {chunk.id: chunk for chunk in chunks}
        self._documents = {document.id: document for document in documents}
        self._lexical = lexical
        self._min_score = min_score
        self._k = k

    def best_lexical_score(self, query: str) -> float:
        return max(self._lexical.scores(query), default=0.0)

    def search(self, query: str) -> SearchOutcome:
        if self.best_lexical_score(query) < self._min_score:
            return SearchOutcome("no_data", ())
        passages = []
        for scored in self._retriever.search(query, self._k):
            document = self._documents[scored.doc_id]
            passages.append(
                Passage(
                    doc_id=document.id,
                    text=self._chunks[scored.chunk_id].text,
                    valid_from=document.valid_from,
                    valid_until=document.valid_until,
                )
            )
        if not passages:
            return SearchOutcome("no_data", ())
        return SearchOutcome("ok", tuple(passages))


class SalesAdapter:
    def __init__(self, service: SalesService) -> None:
        self._service = service

    def lookup(self, raw_date: str) -> SalesOutcome:
        found = self._service.lookup(raw_date)
        record = found.record
        if record is None:
            return SalesOutcome("no_data", found.day, None)
        figures = DayFigures(record.tickets, record.gross, record.refunds, record.net)
        return SalesOutcome("ok", found.day, figures)


class ComplaintsAdapter:
    def __init__(self, service: ComplaintService) -> None:
        self._service = service

    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        outcome = self._service.register(ComplaintDraft(reason, description), conversation_id)
        return Registration(complaint_id=outcome.complaint.id, created=outcome.created)


# ---------------------------------------------------------------------- retrieval


@dataclass(frozen=True, slots=True)
class SearchBuild:
    """The search the agent uses, and which mode is active (the evaluation records it)."""

    search: SearchAdapter
    mode: SearchMode
    reason: str


def build_search(
    settings: Settings, *, today: Callable[[], date], docs_dir: Path | None = None
) -> SearchBuild:
    """Hybrid retrieval when it can work; BM25 with the validity policy otherwise.

    Hybrid needs an embedding for every chunk (from the cache) AND a key, because each query
    must be embedded. Without either, the agent runs in DEGRADED mode: BM25 with the same
    validity demotion. Building never calls the embeddings API: only cached vectors are read.
    """
    documents = load_documents(settings.docs_dir if docs_dir is None else docs_dir)
    chunks = chunk_corpus(documents, AGENT_CHUNKING)
    lexical = BM25Index(chunks, Tokenizer(stem=False))
    bm25 = BM25Retriever(lexical)

    def adapter(retriever: Retriever) -> SearchAdapter:
        return SearchAdapter(retriever, chunks, documents, lexical=lexical)

    def with_validity(base: Retriever) -> Retriever:
        return MetadataRetriever(base, documents, mode="demote", today=today)

    if settings.openai_api_key is None:
        return SearchBuild(adapter(with_validity(bm25)), "degraded", "no_key")
    embedder = build_embedder(settings)
    keys = [embedding_key(embedder.model, chunk.index_text) for chunk in chunks]
    cache = SqliteEmbeddingCache(settings.embeddings_cache_path)
    if len(cache.get_many(keys)) != len(set(keys)):
        return SearchBuild(adapter(with_validity(bm25)), "degraded", "incomplete_cache")
    vectors = embedder.embed([chunk.index_text for chunk in chunks])  # all hits: no API call
    vector = VectorRetriever(VectorIndex(chunks, vectors), embedder)
    hybrid = HybridRetriever([vector, bm25], depth=len(chunks))
    return SearchBuild(adapter(with_validity(hybrid)), "hybrid", "complete")


# ---------------------------------------------------------------------- the agent


@dataclass(frozen=True, slots=True)
class AgentBuild:
    """The customer's side (`service`) and the team's side (`operators`) over ONE store."""

    service: ConversationService
    operators: OperatorService
    search_mode: SearchMode
    search_reason: str


def build_agent_service(
    settings: Settings,
    *,
    clock: Clock,
    model: ChatModel,
    store: ConversationStore,
    hours: BusinessHours | None = None,
    sleeper: Sleeper = time.sleep,
    retry_seed: int = 0,
    docs_dir: Path | None = None,
) -> AgentBuild:
    """The conversation service over the real capabilities, with the simulated failures the
    environment asks for (`TOOL_FAILURE_*`), one fault injector per tool. The attention window
    comes from the settings unless `hours` is given."""
    tz = settings.timezone
    hours = build_business_hours(settings) if hours is None else hours
    search = build_search(settings, today=lambda: business_today(clock, tz), docs_dir=docs_dir)

    sales_repository = FaultySalesRepository(
        JsonSalesRepository(settings.sales_path), FaultInjector(settings.fault_config())
    )
    sales = SalesAdapter(SalesService(sales_repository, clock, tz))

    complaints_store = SqliteComplaintRepository(settings.complaints_db_path)
    complaints_store.initialize()
    complaints_repository = FaultyComplaintRepository(
        complaints_store, FaultInjector(settings.fault_config())
    )
    complaints = ComplaintsAdapter(ComplaintService(complaints_repository, clock))

    executor = ToolExecutor(
        search.search,
        sales,
        complaints,
        sleeper=sleeper,
        # Reproducibility of the backoff jitter, not security.
        rng=random.Random(retry_seed),  # noqa: S311
    )
    service = ConversationService(store, executor, model, hours=hours, clock=clock)
    operators = OperatorService(store, executor, hours=hours, clock=clock)
    return AgentBuild(service, operators, search.mode, search.reason)
