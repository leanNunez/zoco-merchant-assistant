"""Chunking strategies: how a document is cut into the units that get searched.

Chunk size trades precision against context, and the corpus is small enough that the
choice must be measured, not assumed. This module therefore offers three cuts behind one
interface so the evaluation can compare them; it does not pick one.

`text` is what the LLM would read; `index_text` is what is tokenized or embedded. They
differ only when a header is requested, so the header can be tested as a retrieval aid
without ever leaking into the cited text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from merchant_assistant.knowledge.documents import Document

ChunkKind = Literal["document", "section", "window"]


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str
    doc_id: str
    ordinal: int
    heading: str | None
    text: str
    index_text: str


@dataclass(frozen=True, slots=True)
class ChunkingStrategy:
    kind: ChunkKind
    size: int | None = None
    overlap: int = 0
    header: bool = False

    def __post_init__(self) -> None:
        if self.kind == "window":
            if self.size is None or self.size < 1:
                raise ValueError("a window strategy needs size >= 1")
            if not 0 <= self.overlap < self.size:
                # With overlap >= size the window would never advance.
                raise ValueError("overlap must satisfy 0 <= overlap < size")
        elif self.size is not None or self.overlap != 0:
            raise ValueError(f"the {self.kind} strategy takes no size or overlap")

    @property
    def label(self) -> str:
        base: str = f"window-{self.size}-o{self.overlap}" if self.kind == "window" else self.kind
        return f"{base}+header" if self.header else base

    def apply(self, document: Document) -> tuple[Chunk, ...]:
        if self.kind == "document":
            pieces: list[tuple[str | None, str]] = [(None, document.text)]
        elif self.kind == "section":
            pieces = [(section.heading or None, section.text) for section in document.sections]
        else:
            pieces = [(None, text) for text in self._windows(document)]
        return tuple(
            Chunk(
                id=f"{document.id}#{ordinal}",
                doc_id=document.id,
                ordinal=ordinal,
                heading=heading,
                text=text,
                index_text=self._index_text(document, text),
            )
            for ordinal, (heading, text) in enumerate(pieces)
        )

    def _windows(self, document: Document) -> list[str]:
        assert self.size is not None  # noqa: S101 - guaranteed by __post_init__ for windows
        words = document.text.split()
        stride = self.size - self.overlap
        windows: list[str] = []
        start = 0
        while words:
            windows.append(" ".join(words[start : start + self.size]))
            if start + self.size >= len(words):
                break
            start += stride
        return windows

    def _index_text(self, document: Document, text: str) -> str:
        if not self.header:
            return text
        # The title, type and validity travel with every chunk so a fragment of a historical
        # document still says that it is historical when it is embedded or tokenized.
        return (
            f"{document.title} | {document.kind} | "
            f"vigente {document.valid_from} a {document.valid_until}\n{text}"
        )


def chunk_corpus(documents: Iterable[Document], strategy: ChunkingStrategy) -> tuple[Chunk, ...]:
    return tuple(chunk for document in documents for chunk in strategy.apply(document))
