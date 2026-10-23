"""The embedder used when no API key is configured.

It lets everything that is already cached work offline: `CachedEmbedder` only calls it for
texts the cache does not have, and then it fails with an error that says how to fix it.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError


class MissingKeyEmbedder:
    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        raise EmbeddingsUnavailableError(
            f"{len(texts)} texts are not in the embeddings cache and no API key is "
            "configured: set OPENAI_API_KEY in .env"
        )
