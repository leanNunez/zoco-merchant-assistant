"""Composition root: the only place that wires adapters to ports. The evaluation reuses it.

The agent reaches the capabilities only through its own ports (`agent/ports.py`); the thin
adapter classes here translate between the two vocabularies, so neither side imports the
other and import-linter can keep it that way.
"""

from __future__ import annotations

import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import openai
from pydantic import SecretStr

from merchant_assistant.agent.adapters.errors import LLMNotConfigured
from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.openai_chat_model import PROFILES, OpenAIChatModel
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
from merchant_assistant.shared.clock import Clock, SystemClock, business_today
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


SDK_RETRIES = 2  # the only retry layer: the agent adds none of its own (ADR-0005)


def build_chat_model(settings: Settings) -> OpenAIChatModel:
    """The chat model of the settings, against OpenAI or any OpenAI-compatible endpoint.

    Without a key it refuses to build, naming the variables to set and no value.
    """
    key = settings.effective_llm_api_key()
    if key is None:
        raise LLMNotConfigured("no key for the chat model")
    client = openai.OpenAI(
        api_key=key.get_secret_value(),
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=SDK_RETRIES,
    )
    return OpenAIChatModel(
        client,
        settings.llm_model,
        profile=PROFILES[settings.llm_provider],
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        strict_tools=settings.llm_strict_tools,
    )


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
    sales_faults: FaultInjector | None = None,
    complaint_faults: FaultInjector | None = None,
) -> AgentBuild:
    """The conversation service over the real capabilities, with the simulated failures the
    environment asks for (`TOOL_FAILURE_*`), one fault injector per tool. The attention window
    comes from the settings unless `hours` is given.

    `sales_faults` and `complaint_faults` replace the injector built from the settings for one
    tool. Production never passes them; the conversation evaluation does, to make a tool fail
    in an exact, scripted way (time out twice, then work) while everything else is the
    production composition."""
    tz = settings.timezone
    hours = build_business_hours(settings) if hours is None else hours
    search = build_search(settings, today=lambda: business_today(clock, tz), docs_dir=docs_dir)

    sales_injector = (
        FaultInjector(settings.fault_config()) if sales_faults is None else sales_faults
    )
    sales_repository = FaultySalesRepository(
        JsonSalesRepository(settings.sales_path), sales_injector
    )
    sales = SalesAdapter(SalesService(sales_repository, clock, tz))

    complaints_store = SqliteComplaintRepository(settings.complaints_db_path)
    complaints_store.initialize()
    complaint_injector = (
        FaultInjector(settings.fault_config()) if complaint_faults is None else complaint_faults
    )
    complaints_repository = FaultyComplaintRepository(complaints_store, complaint_injector)
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


# ---------------------------------------------------------------------- the console

PIN_DIGITS = 6


@dataclass(frozen=True, slots=True)
class ConsoleRuntime:
    """Everything the console needs, built once.

    The customer, the operator and the supervisor act on ONE store in ONE process: the
    confirmation proof (`Confirmed`) lives in memory by design, so it cannot cross processes.
    The PINs are secrets: they stay out of reprs and logs.
    """

    service: ConversationService
    operators: OperatorService
    store: ConversationStore
    hours: BusinessHours
    clock: Clock
    search_mode: SearchMode
    search_reason: str
    llm_label: str  # provider and model, never a key
    operator_pin: SecretStr
    supervisor_pin: SecretStr
    # The roles whose PIN was generated: the console shows those, once, in its DEMO banner. A
    # configured PIN is never shown.
    generated_pins: frozenset[str]
    # Every value that must never leave the process: both PINs and the API keys. The console
    # refuses to forward a customer message that contains one (ADR-0005). Never printed or
    # logged, and out of the repr like the PINs.
    protected_secrets: tuple[str, ...] = field(repr=False)


def build_console_runtime(
    settings: Settings,
    *,
    model: ChatModel | None = None,
    clock: Clock | None = None,
    store: ConversationStore | None = None,
    hours: BusinessHours | None = None,
    sleeper: Sleeper = time.sleep,
    docs_dir: Path | None = None,
) -> ConsoleRuntime:
    """The agent plus the team's side over one in-memory store, ready for the console.

    Tests inject `model` (a scripted one), `clock` and `store`. Without a `model`, the chat model
    of the settings is built first, so a missing key fails before anything else is touched.
    """
    chat_model = build_chat_model(settings) if model is None else model
    label = f"{settings.llm_provider}:{settings.llm_model}" if model is None else "modelo inyectado"
    the_clock = SystemClock() if clock is None else clock
    the_store: ConversationStore = InMemoryConversationStore() if store is None else store
    built = build_agent_service(
        settings,
        clock=the_clock,
        model=chat_model,
        store=the_store,
        hours=hours,
        sleeper=sleeper,
        docs_dir=docs_dir,
    )
    operator_pin, supervisor_pin, generated = _console_pins(settings)
    return ConsoleRuntime(
        service=built.service,
        operators=built.operators,
        store=the_store,
        hours=build_business_hours(settings) if hours is None else hours,
        clock=the_clock,
        search_mode=built.search_mode,
        search_reason=built.search_reason,
        llm_label=label,
        operator_pin=operator_pin,
        supervisor_pin=supervisor_pin,
        generated_pins=generated,
        protected_secrets=_protected_secrets(settings, operator_pin, supervisor_pin),
    )


def _protected_secrets(
    settings: Settings, operator_pin: SecretStr, supervisor_pin: SecretStr
) -> tuple[str, ...]:
    """The values that must not reach the model, the history or the log: the PINs in use and
    the API keys of the chat model and of the embeddings, when there are any."""
    wrapped = (
        operator_pin,
        supervisor_pin,
        settings.effective_llm_api_key(),
        settings.llm_api_key,
        settings.openai_api_key,
    )
    values = (secret.get_secret_value() for secret in wrapped if secret is not None)
    return tuple(dict.fromkeys(value for value in values if value))


def _console_pins(settings: Settings) -> tuple[SecretStr, SecretStr, frozenset[str]]:
    """The configured PINs; a role without one gets a random PIN for this run.

    A role switch of a simulator, not authentication (ADR-0005). Generated PINs never repeat
    another role's PIN.
    """
    configured = {"operator": settings.operator_pin, "supervisor": settings.supervisor_pin}
    taken = {pin.get_secret_value() for pin in configured.values() if pin is not None}
    pins: dict[str, SecretStr] = {}
    generated: set[str] = set()
    for role, pin in configured.items():
        if pin is None:
            fresh = _random_pin()
            while fresh in taken:
                fresh = _random_pin()
            taken.add(fresh)
            pin = SecretStr(fresh)
            generated.add(role)
        pins[role] = pin
    return pins["operator"], pins["supervisor"], frozenset(generated)


def _random_pin() -> str:
    return f"{secrets.randbelow(10**PIN_DIGITS):0{PIN_DIGITS}d}"
