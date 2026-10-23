"""What a result was computed from, so a reader can tell whether it still applies."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Provenance:
    embeddings_model: str
    reference_date: date
    corpus_sha256: str
    chunk_counts: dict[str, int]
    api_calls: int


def corpus_digest(files: Sequence[tuple[str, bytes]]) -> str:
    """sha256 over each file in path order: its relative path, NUL, byte length, NUL, bytes.

    The path is hashed so that renaming a file changes the digest too, and the length
    keeps the boundary between two files unambiguous.
    """
    digest = hashlib.sha256()
    for path, content in sorted(files):
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()
