"""Embeddings from the OpenAI API.

The client is taken as an argument (anything shaped like `openai.OpenAI`), so the tests
pass a small fake and never touch the network. Every SDK failure becomes an
`EmbeddingsUnavailableError`: the caller sees a stable code and a safe message, and the
SDK class and text stay in `detail`, for the logs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
import openai
from numpy.typing import NDArray

from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError, InvalidEmbeddingError


class _EmbeddingItem(Protocol):
    @property
    def embedding(self) -> Sequence[float]: ...

    @property
    def index(self) -> int: ...


class _EmbeddingResponse(Protocol):
    @property
    def data(self) -> Sequence[_EmbeddingItem]: ...


class _EmbeddingsResource(Protocol):
    def create(self, *, input: list[str], model: str) -> _EmbeddingResponse: ...


class EmbeddingsClient(Protocol):
    @property
    def embeddings(self) -> _EmbeddingsResource: ...


class OpenAIEmbedder:
    def __init__(self, client: EmbeddingsClient, model: str, *, batch_size: int = 256) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._client = client
        self._model = model
        self._batch_size = batch_size
        self.requests = 0

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        rows: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            rows.extend(self._embed_batch(list(texts[start : start + self._batch_size])))
        if not rows:
            return np.zeros((0, 0), dtype=np.float64)
        dimensions = {len(row) for row in rows}
        if len(dimensions) != 1:
            raise InvalidEmbeddingError(f"rows of different dimensions: {sorted(dimensions)}")
        matrix = np.array(rows, dtype=np.float64)
        if matrix.shape[1] == 0:
            raise InvalidEmbeddingError("the provider returned empty vectors")
        if not np.isfinite(matrix).all():
            raise InvalidEmbeddingError("the provider returned NaN or infinite values")
        return matrix

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        self.requests += 1
        try:
            response = self._client.embeddings.create(input=batch, model=self._model)
        except openai.OpenAIError as error:
            raise EmbeddingsUnavailableError(f"{type(error).__name__}: {error}") from error
        # The API documents `index` as the position in the input; ordering by it, instead of
        # trusting the order of `data`, keeps a vector from being paired with the wrong text.
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(batch))):
            raise InvalidEmbeddingError(
                f"asked for {len(batch)} vectors, got indexes {[item.index for item in items]}"
            )
        return [list(item.embedding) for item in items]
