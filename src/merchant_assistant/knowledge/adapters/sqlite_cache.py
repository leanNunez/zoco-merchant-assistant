"""The embeddings cache, in one SQLite file.

The file is committed with the repository, so the evaluation reproduces offline and
without an API key. Writes are idempotent: a key that is already stored is never
overwritten, so two processes racing to store the same vector cannot corrupt it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from merchant_assistant.knowledge.errors import InvalidEmbeddingError

# Little-endian float32. The provider's vectors carry float32-level precision anyway, so
# storing float64 would double the committed file without changing a single ranking.
_STORED = np.dtype("<f4")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
)
"""


class SqliteEmbeddingCache:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute(_SCHEMA)

    def get_many(self, keys: Sequence[str]) -> dict[str, NDArray[np.float64]]:
        found: dict[str, NDArray[np.float64]] = {}
        with closing(sqlite3.connect(self._path)) as connection:
            for key in dict.fromkeys(keys):
                row = connection.execute(
                    "SELECT dim, vector FROM embeddings WHERE key = ?", (key,)
                ).fetchone()
                if row is not None:
                    found[key] = _decode(key, row[0], row[1])
        return found

    def put_many(self, items: Mapping[str, NDArray[np.float64]], *, model: str) -> None:
        rows = [(key, model, *_encode(key, vector)) for key, vector in items.items()]
        # One transaction for the whole batch: either every vector is stored or none is.
        with self._transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO embeddings (key, model, dim, vector) VALUES (?, ?, ?, ?)",
                rows,
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # `with connection` commits or rolls back but does not close; `closing` does.
        with closing(sqlite3.connect(self._path)) as connection, connection:
            yield connection


def _encode(key: str, vector: NDArray[np.float64]) -> tuple[int, bytes]:
    values = np.asarray(vector, dtype=np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise InvalidEmbeddingError(f"refusing to cache an unusable vector for key {key}")
    return int(values.size), values.astype(_STORED).tobytes()


def _decode(key: str, dim: int, blob: bytes) -> NDArray[np.float64]:
    vector = np.frombuffer(blob, dtype=_STORED)
    if vector.size != dim:
        raise InvalidEmbeddingError(f"cached vector {key} has {vector.size} values, expected {dim}")
    return vector.astype(np.float64)
