"""`buscar_documentacion` over each company's own documents, reusing the first delivery's search.

Each company has its own library (BM25 over whole documents, as the first delivery chose), and an
adapter is bound to one company: it cannot reach another company's documents. A company with no
documents gets an empty answer, never somebody else's. Documents are reference data: the
envelope the model receives marks them untrusted, and nothing in a document is ever executed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel

from cortex.identity import IDENTIFIER, AuthContext
from merchant_assistant.agent.domain.tools import BuscarDocumentacionArgs
from merchant_assistant.knowledge.adapters.filesystem import load_documents
from merchant_assistant.knowledge.bm25 import BM25Index
from merchant_assistant.knowledge.chunking import Chunk, ChunkingStrategy, chunk_corpus
from merchant_assistant.knowledge.documents import Document
from merchant_assistant.knowledge.text import Tokenizer

TOP_K = 3


class _Library:
    def __init__(self, documents: tuple[Document, ...]) -> None:
        self.documents = {document.id: document for document in documents}
        self.chunks: Mapping[str, Chunk] = MappingProxyType(
            {chunk.id: chunk for chunk in chunk_corpus(documents, ChunkingStrategy("document"))}
        )
        self.index = BM25Index(tuple(self.chunks.values()), Tokenizer())


class DocsLibrary:
    def __init__(self, corpora: Mapping[str, Path]) -> None:
        libraries: dict[str, _Library] = {}
        for tenant_id, directory in corpora.items():
            if IDENTIFIER.fullmatch(tenant_id) is None:
                raise ValueError(f"not a valid tenant id: {tenant_id!r}")
            libraries[tenant_id] = _Library(load_documents(directory))
        self._libraries = MappingProxyType(libraries)

    def for_tenant(self, auth: AuthContext) -> DocsAdapter:
        return DocsAdapter(auth.tenant_id, self._libraries.get(auth.tenant_id))


class DocsAdapter:
    def __init__(self, tenant_id: str, library: _Library | None) -> None:
        self.tenant_id = tenant_id
        self._library = library

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        del conversation_id  # a document search does not depend on the conversation
        if not isinstance(arguments, BuscarDocumentacionArgs):
            raise TypeError("buscar_documentacion received arguments of another tool")
        if self._library is None:
            return {"chunks": []}
        found = []
        for hit in self._library.index.search(arguments.consulta, TOP_K):
            document = self._library.documents[hit.doc_id]
            found.append(
                {
                    "doc_id": document.id,
                    "titulo": document.title,
                    "vigencia_desde": document.valid_from.isoformat(),
                    "vigencia_hasta": document.valid_until.isoformat(),
                    "texto": self._library.chunks[hit.chunk_id].text,
                }
            )
        return {"chunks": found}
