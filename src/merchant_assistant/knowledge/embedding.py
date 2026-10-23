"""Embeddings through a content-addressed cache.

The key is a hash of the model name and the exact text, so a vector is reused only when
the same model would produce it for the same input: editing a chunk or switching models
changes the key, and a stale vector can never be served. Running the evaluation twice
therefore pays the provider once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from merchant_assistant.knowledge.errors import InvalidEmbeddingError
from merchant_assistant.knowledge.ports import Embedder, EmbeddingCache


def embedding_key(model: str, text: str) -> str:
    # Model names never contain a newline, so the first newline always ends the model name
    # and two different (model, text) pairs cannot produce the same string. The text is not
    # normalized: whatever changes the provider's input must change the key.
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


class CachedEmbedder:
    """An `Embedder` that asks the inner one only for texts the cache does not have.

    The counters exist so that "the second run did not call the API" is something the
    evaluation can print and record, not a claim:

    - `hits` / `misses`: distinct texts found / not found in the cache.
    - `inner_calls`: calls made to the inner embedder (at most one per `embed` call).
    """

    def __init__(self, inner: Embedder, cache: EmbeddingCache) -> None:
        self._inner = inner
        self._cache = cache
        self.hits = 0
        self.misses = 0
        self.inner_calls = 0

    @property
    def model(self) -> str:
        return self._inner.model

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        keys = [embedding_key(self.model, text) for text in texts]
        text_by_key = dict(zip(keys, texts, strict=True))  # also drops repeated texts
        vectors = self._cache.get_many(list(text_by_key))
        missing = [key for key in text_by_key if key not in vectors]
        self.hits += len(text_by_key) - len(missing)
        self.misses += len(missing)
        if missing:
            self.inner_calls += 1
            fresh = self._inner.embed([text_by_key[key] for key in missing])
            if fresh.ndim != 2 or fresh.shape[0] != len(missing):
                raise InvalidEmbeddingError(
                    f"asked for {len(missing)} vectors, got shape {fresh.shape}"
                )
            self._cache.put_many(dict(zip(missing, fresh, strict=True)), model=self.model)
            # Read them back instead of using `fresh`: the cache may store them with less
            # precision, and a text must get the same vector whether it was a hit or a miss.
            vectors.update(self._cache.get_many(missing))
        absent = [key for key in text_by_key if key not in vectors]
        if absent:
            raise InvalidEmbeddingError(f"the cache lost {len(absent)} vectors it just stored")
        if not keys:
            return np.zeros((0, 0), dtype=np.float64)
        dimensions = {vectors[key].shape for key in text_by_key}
        if len(dimensions) != 1:
            raise InvalidEmbeddingError(f"vectors of different shapes: {sorted(dimensions)}")
        return np.stack([vectors[key] for key in keys]).astype(np.float64, copy=False)
