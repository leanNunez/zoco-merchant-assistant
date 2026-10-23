"""What the retrieval domain needs from the outside world, and nothing more.

The domain depends on these protocols; the adapters (the OpenAI client, the SQLite cache)
implement them. Tests implement them too, with a few lines and no network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class Embedder(Protocol):
    @property
    def model(self) -> str:
        """The name of the model that produces the vectors: it is part of the cache key."""
        ...

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        """One row per text, in input order: shape `(len(texts), d)`."""
        ...


class EmbeddingCache(Protocol):
    def get_many(self, keys: Sequence[str]) -> dict[str, NDArray[np.float64]]:
        """The stored vectors of the keys that are present; absent keys are left out."""
        ...

    def put_many(self, items: Mapping[str, NDArray[np.float64]], *, model: str) -> None:
        """Store vectors produced by `model`. A key that is already stored is kept as is."""
        ...
