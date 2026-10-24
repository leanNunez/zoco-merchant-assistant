"""The retrieval strategies behind one interface, so the evaluation can compare them.

- `BM25Retriever` and `VectorRetriever` wrap an index.
- `HybridRetriever` fuses the rankings of several retrievers with RRF, by chunk (the default)
  or by document (`fuse_by="document"`).
- `MetadataRetriever` applies the validity policy: it filters out, or demotes, chunks of
  documents that were not valid during the period the question is about.
- `KindDemotionRetriever` is an ablation: it demotes a document `tipo` regardless of the
  dates, to show whether `tipo` says anything that validity does not already say.

Every `search` returns at most `k` chunks, best first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Literal, Protocol

from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.fusion import reciprocal_rank_fusion
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.knowledge.ranking import ScoredChunk, best_chunk_per_document
from merchant_assistant.knowledge.temporal import is_valid_during, reference_period
from merchant_assistant.knowledge.vector import VectorIndex

MetadataMode = Literal["filter", "demote"]
FusionUnit = Literal["chunk", "document"]


class Retriever(Protocol):
    @property
    def name(self) -> str: ...

    def search(self, query: str, k: int) -> list[ScoredChunk]: ...


class BM25Retriever:
    def __init__(self, index: BM25Index, *, name: str = "bm25") -> None:
        self._index = index
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        return self._index.search(query, k)


class VectorRetriever:
    def __init__(self, index: VectorIndex, embedder: Embedder, *, name: str = "vector") -> None:
        self._index = index
        self._embedder = embedder
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        _validate_k(k)
        return self._index.search(self._embedder.embed([query])[0], k)


class HybridRetriever:
    """RRF over the top `depth` chunks of each retriever.

    `depth` is how far down each list the fusion looks. A chunk only one retriever finds
    still gets that retriever's contribution, so lexical and semantic matches both count.

    `fuse_by` is the unit that is fused. With `"chunk"` (the default) every chunk is a
    candidate of its own. With `"document"` each retriever's ranking is first reduced to its
    documents (a document sits at the position of its best chunk, so a document with many
    chunks cannot collect several contributions from one list), the document ids are fused,
    and each fused document is represented by its best chunk: the one ranked highest in any
    list, the earlier retriever winning a tie.
    """

    def __init__(
        self,
        retrievers: Sequence[Retriever],
        *,
        depth: int,
        rrf_k: int = 60,
        name: str = "hybrid",
        fuse_by: FusionUnit = "chunk",
    ) -> None:
        if not retrievers:
            raise ValueError("a hybrid retriever needs at least one retriever")
        if depth < 1:
            raise ValueError("depth must be >= 1")
        if fuse_by not in ("chunk", "document"):
            raise ValueError("fuse_by must be 'chunk' or 'document'")
        self._retrievers = tuple(retrievers)
        self._depth = depth
        self._rrf_k = rrf_k
        self._name = name
        self._fuse_by: FusionUnit = fuse_by

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        _validate_k(k)
        results = [retriever.search(query, self._depth) for retriever in self._retrievers]
        if self._fuse_by == "document":
            return self._fuse_documents(results)[:k]
        doc_of_chunk: dict[str, str] = {}
        rankings: list[list[str]] = []
        for found in results:
            doc_of_chunk.update((result.chunk_id, result.doc_id) for result in found)
            rankings.append([result.chunk_id for result in found])
        fused = reciprocal_rank_fusion(rankings, k=self._rrf_k)
        return [ScoredChunk(chunk_id, doc_of_chunk[chunk_id], score) for chunk_id, score in fused][
            :k
        ]

    def _fuse_documents(self, results: list[list[ScoredChunk]]) -> list[ScoredChunk]:
        reduced = [best_chunk_per_document(found) for found in results]
        # The representative of a document: the chunk it had at the best position anywhere.
        representative: dict[str, tuple[int, int, str]] = {}
        for source, found in enumerate(reduced):
            for rank, chunk in enumerate(found, start=1):
                candidate = (rank, source, chunk.chunk_id)
                if chunk.doc_id not in representative or candidate < representative[chunk.doc_id]:
                    representative[chunk.doc_id] = candidate
        fused = reciprocal_rank_fusion(
            [[chunk.doc_id for chunk in found] for found in reduced], k=self._rrf_k
        )
        return [ScoredChunk(representative[doc_id][2], doc_id, score) for doc_id, score in fused]


class MetadataRetriever:
    """The validity policy on top of another retriever.

    The period comes from the question (a month, a date, a year) or, if it names none,
    from `today()`. In `filter` mode the chunks of documents not valid in that period are
    dropped, and an empty result is an honest "there is no data for that period". In
    `demote` mode nothing is dropped: valid documents come first and invalid ones after,
    each group in the base order.
    """

    def __init__(
        self,
        base: Retriever,
        documents: Iterable[Document],
        *,
        mode: MetadataMode,
        today: Callable[[], date],
    ) -> None:
        self._base = base
        self._documents = {document.id: document for document in documents}
        self._mode: MetadataMode = mode
        self._today = today

    @property
    def name(self) -> str:
        return f"{self._base.name}+meta({self._mode})"

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        _validate_k(k)
        period = reference_period(query, self._today())

        def is_valid(doc_id: str) -> bool:
            return is_valid_during(_document(self._documents, doc_id), period)

        return _preferring(self._base, query, k, is_valid, drop_others=self._mode == "filter")


class KindDemotionRetriever:
    """An ablation: documents of one `tipo` go last, whatever their dates say."""

    def __init__(
        self, base: Retriever, documents: Iterable[Document], *, kind: str = "historico"
    ) -> None:
        self._base = base
        self._documents = {document.id: document for document in documents}
        self._kind = kind

    @property
    def name(self) -> str:
        return f"{self._base.name}+kind-demotion({self._kind})"

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        _validate_k(k)

        def is_preferred(doc_id: str) -> bool:
            return _document(self._documents, doc_id).kind != self._kind

        return _preferring(self._base, query, k, is_preferred, drop_others=False)


def _preferring(
    base: Retriever,
    query: str,
    k: int,
    is_preferred: Callable[[str], bool],
    *,
    drop_others: bool,
) -> list[ScoredChunk]:
    """The base results with preferred documents first (and the rest dropped, if asked).

    Reordering only the base's top `k` would miss a preferred chunk ranked just below it,
    so the base is asked for more until `k` preferred chunks are found or it runs out.
    """
    depth = k
    while True:
        results = base.search(query, depth)
        preferred = [result for result in results if is_preferred(result.doc_id)]
        if len(preferred) >= k or len(results) < depth:
            break
        depth *= 2
    if drop_others:
        return preferred[:k]
    others = [result for result in results if not is_preferred(result.doc_id)]
    return [*preferred, *others][:k]


def _document(documents: dict[str, Document], doc_id: str) -> Document:
    try:
        return documents[doc_id]
    except KeyError:
        # A result for a document the policy does not know cannot be judged valid or not.
        raise ValueError(f"no metadata for document {doc_id}") from None


def _validate_k(k: int) -> None:
    if k < 1:
        raise ValueError("k must be >= 1")
