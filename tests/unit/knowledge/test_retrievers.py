"""The retrieval strategies, on literal rankings and on a three-document mini-corpus."""

import hashlib
from collections.abc import Sequence
from datetime import date

import numpy as np
import pytest
from numpy.typing import NDArray

from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document, Section
from merchant_assistant.knowledge.ranking import ScoredChunk, document_ranking
from merchant_assistant.knowledge.retrievers import (
    BM25Retriever,
    HybridRetriever,
    KindDemotionRetriever,
    MetadataMode,
    MetadataRetriever,
    Retriever,
    VectorRetriever,
)
from merchant_assistant.knowledge.text import Tokenizer
from merchant_assistant.knowledge.vector import VectorIndex

TODAY = date(2026, 10, 23)

# A current tariff and the historical one it replaced share most of their vocabulary;
# a third document talks about something else.
CURRENT = Document(
    id="CUR",
    title="Tarifas vigentes",
    kind="tarifario",
    valid_from=date(2026, 9, 1),
    valid_until=date(2027, 3, 31),
    sections=(Section("Tarifas", "el plan pro cobra cuatro por ciento por entrada"),),
)
HISTORICAL = Document(
    id="HIS",
    title="Tarifas historicas",
    kind="historico",
    valid_from=date(2026, 1, 1),
    valid_until=date(2026, 8, 31),
    sections=(Section("Tarifas", "el plan pro cobraba cinco por ciento por entrada"),),
)
OTHER = Document(
    id="OTH",
    title="Transferencias",
    kind="ayuda",
    valid_from=date(2026, 1, 1),
    valid_until=date(2027, 12, 31),
    sections=(Section("Transferencias", "una entrada puede transferirse una vez"),),
)
CORPUS = (CURRENT, HISTORICAL, OTHER)


class HashingEmbedder:
    """Bag of words hashed into 32 dimensions: deterministic, no network, rough semantics."""

    model = "hashing-32"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        self.calls.append(list(texts))
        rows = np.zeros((len(texts), 32))
        for row, text in zip(rows, texts, strict=True):
            row[0] = 1.0  # a constant component, so no text maps to the zero vector
            for token in Tokenizer()(text):
                row[int(hashlib.sha256(token.encode()).hexdigest(), 16) % 31 + 1] += 1.0
        return rows


class StaticRetriever:
    """Returns a fixed ranking, so fusion and policies can be checked by hand."""

    def __init__(self, name: str, chunk_ids: Sequence[str]) -> None:
        self.name = name
        self._results = [
            ScoredChunk(chunk_id, chunk_id.split("#")[0], 1.0 / (rank + 1))
            for rank, chunk_id in enumerate(chunk_ids)
        ]
        self.depths: list[int] = []

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        self.depths.append(k)
        return self._results[:k]


def ids(results: Sequence[ScoredChunk]) -> list[str]:
    return [result.chunk_id for result in results]


def today() -> date:
    return TODAY


# --- BM25 and vector -------------------------------------------------------------------


def mini_bm25() -> BM25Retriever:
    return BM25Retriever(BM25Index(chunk_corpus(CORPUS, ChunkingStrategy("document")), Tokenizer()))


def mini_vector(embedder: HashingEmbedder) -> VectorRetriever:
    chunks = chunk_corpus(CORPUS, ChunkingStrategy("document"))
    return VectorRetriever(
        VectorIndex(chunks, embedder.embed([c.index_text for c in chunks])), embedder
    )


def test_bm25_ranks_the_document_that_has_the_rare_word_first() -> None:
    # "cobraba" and "cinco" only appear in HIS; OTH shares no word with the query.
    assert ids(mini_bm25().search("cuanto cobraba el plan pro", k=3)) == ["HIS#0", "CUR#0"]


def test_bm25_has_the_default_name_unless_told_otherwise() -> None:
    index = BM25Index([], Tokenizer())

    assert BM25Retriever(index).name == "bm25"
    assert BM25Retriever(index, name="bm25(stem)").name == "bm25(stem)"


def test_the_vector_retriever_embeds_the_query_with_the_embedder_it_was_given() -> None:
    embedder = HashingEmbedder()
    retriever = mini_vector(embedder)

    results = retriever.search("una entrada puede transferirse una vez", k=1)

    assert embedder.calls[-1] == ["una entrada puede transferirse una vez"]
    assert ids(results) == ["OTH#0"]
    assert retriever.name == "vector"


def test_the_vector_retriever_returns_every_chunk_when_k_covers_the_corpus() -> None:
    results = mini_vector(HashingEmbedder()).search("plan pro", k=10)

    assert sorted(ids(results)) == ["CUR#0", "HIS#0", "OTH#0"]


# --- hybrid ------------------------------------------------------------------------------


def test_hybrid_fuses_chunk_rankings_with_rrf_scores_computed_by_hand() -> None:
    first = StaticRetriever("a", ["A#0", "B#0", "C#0"])
    second = StaticRetriever("b", ["B#0", "D#0"])

    results = HybridRetriever([first, second], depth=3, rrf_k=60).search("q", k=4)

    # B#0: 1/62 + 1/61 = 0.032522; A#0: 1/61 = 0.016393; D#0: 1/62 = 0.016129; C#0: 1/63
    assert ids(results) == ["B#0", "A#0", "D#0", "C#0"]
    assert [r.score for r in results] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 62, 1 / 63])
    assert [r.doc_id for r in results] == ["B", "A", "D", "C"]


def test_hybrid_asks_each_retriever_for_depth_results_and_returns_at_most_k() -> None:
    first = StaticRetriever("a", ["A#0", "B#0", "C#0"])
    second = StaticRetriever("b", ["C#0", "B#0", "A#0"])

    results = HybridRetriever([first, second], depth=2).search("q", k=1)

    assert (first.depths, second.depths) == ([2], [2])
    # depth 2: A#0 and C#0 each get 1/61 once, B#0 gets 1/62 twice = 0.032258 > 0.016393
    assert ids(results) == ["B#0"]


def test_hybrid_fuses_chunks_not_documents() -> None:
    first = StaticRetriever("a", ["A#0", "A#1"])

    assert ids(HybridRetriever([first], depth=2).search("q", k=2)) == ["A#0", "A#1"]


def test_hybrid_over_the_mini_corpus_puts_the_lexical_and_semantic_winner_first() -> None:
    embedder = HashingEmbedder()
    hybrid = HybridRetriever([mini_vector(embedder), mini_bm25()], depth=3)

    results = hybrid.search("una entrada puede transferirse una vez", k=3)

    assert ids(results)[0] == "OTH#0"
    assert hybrid.name == "hybrid"


@pytest.mark.parametrize(("depth", "fragment"), [(0, "depth"), (-1, "depth")])
def test_hybrid_refuses_a_depth_below_one(depth: int, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        HybridRetriever([StaticRetriever("a", [])], depth=depth)


def test_hybrid_needs_at_least_one_retriever() -> None:
    with pytest.raises(ValueError, match="at least one"):
        HybridRetriever([], depth=1)


# --- validity metadata -------------------------------------------------------------------

# The base ranks the historical document first, as BM25 does for "cobraba".
BASE_ORDER = ["HIS#0", "CUR#0", "OTH#0"]


def meta(mode: MetadataMode) -> MetadataRetriever:
    return MetadataRetriever(StaticRetriever("hybrid", BASE_ORDER), CORPUS, mode=mode, today=today)


def test_filter_without_a_period_in_the_question_drops_documents_not_valid_today() -> None:
    assert ids(meta("filter").search("cuanto cobra el plan pro", k=3)) == ["CUR#0", "OTH#0"]


def test_filter_with_a_past_month_keeps_the_documents_valid_then() -> None:
    # In July 2026 HIS and OTH were valid; CUR started in September.
    results = meta("filter").search("cuanto cobraba el plan pro en julio de 2026", k=3)

    assert ids(results) == ["HIS#0", "OTH#0"]


def test_filter_returns_nothing_when_no_document_was_valid_in_the_period() -> None:
    assert meta("filter").search("tarifas en 2020", k=3) == []


def test_demote_keeps_every_document_but_moves_the_invalid_ones_last() -> None:
    assert ids(meta("demote").search("cuanto cobra el plan pro", k=3)) == [
        "CUR#0",
        "OTH#0",
        "HIS#0",
    ]


def test_demote_keeps_the_base_order_inside_each_group() -> None:
    base = StaticRetriever("hybrid", ["HIS#0", "OTH#0", "HIS#1", "CUR#0"])

    results = MetadataRetriever(base, CORPUS, mode="demote", today=today).search("q", k=4)

    assert ids(results) == ["OTH#0", "CUR#0", "HIS#0", "HIS#1"]


def test_a_valid_document_just_below_the_top_k_of_the_base_is_still_found() -> None:
    base = StaticRetriever("hybrid", ["HIS#0", "HIS#1", "HIS#2", "CUR#0"])

    results = MetadataRetriever(base, CORPUS, mode="filter", today=today).search("q", k=1)

    assert ids(results) == ["CUR#0"]
    assert base.depths == [1, 2, 4]  # asked for more until a valid chunk appeared


def test_the_reference_date_is_read_at_search_time() -> None:
    days = iter([date(2026, 7, 1), date(2026, 10, 1)])
    retriever = MetadataRetriever(
        StaticRetriever("hybrid", BASE_ORDER), CORPUS, mode="filter", today=lambda: next(days)
    )

    assert ids(retriever.search("q", k=3)) == ["HIS#0", "OTH#0"]
    assert ids(retriever.search("q", k=3)) == ["CUR#0", "OTH#0"]


def test_the_metadata_retriever_is_named_after_its_base_and_mode() -> None:
    assert meta("filter").name == "hybrid+meta(filter)"
    assert meta("demote").name == "hybrid+meta(demote)"


def test_a_result_for_a_document_without_metadata_is_refused() -> None:
    base = StaticRetriever("hybrid", ["NOPE#0"])

    with pytest.raises(ValueError, match="no metadata for document NOPE"):
        MetadataRetriever(base, CORPUS, mode="filter", today=today).search("q", k=1)


def test_the_policy_works_end_to_end_on_a_real_hybrid_of_the_mini_corpus() -> None:
    hybrid = HybridRetriever([mini_vector(HashingEmbedder()), mini_bm25()], depth=3)
    retriever = MetadataRetriever(hybrid, CORPUS, mode="filter", today=today)

    july = document_ranking(retriever.search("cuanto cobraba el plan pro en julio de 2026", 3))
    now = document_ranking(retriever.search("cuanto cobraba el plan pro", 3))

    assert "CUR" not in july
    assert "HIS" not in now


# --- kind demotion (ablation) ------------------------------------------------------------


def test_kind_demotion_moves_the_historical_kind_last_whatever_the_period() -> None:
    retriever = KindDemotionRetriever(StaticRetriever("hybrid", BASE_ORDER), CORPUS)

    # Even for July 2026, when HIS was the valid one: the kind ignores dates.
    results = retriever.search("cuanto cobraba el plan pro en julio de 2026", k=3)

    assert ids(results) == ["CUR#0", "OTH#0", "HIS#0"]
    assert retriever.name == "hybrid+kind-demotion(historico)"


def test_kind_demotion_can_target_another_kind() -> None:
    retriever = KindDemotionRetriever(StaticRetriever("hybrid", BASE_ORDER), CORPUS, kind="ayuda")

    assert ids(retriever.search("q", k=3)) == ["HIS#0", "CUR#0", "OTH#0"]


@pytest.mark.parametrize(
    "retriever",
    [
        meta("filter"),
        KindDemotionRetriever(StaticRetriever("hybrid", BASE_ORDER), CORPUS),
        HybridRetriever([StaticRetriever("a", BASE_ORDER)], depth=1),
    ],
    ids=["meta", "kind", "hybrid"],
)
def test_searching_for_fewer_than_one_result_is_refused(retriever: Retriever) -> None:
    with pytest.raises(ValueError, match="k must be"):
        retriever.search("q", k=0)
