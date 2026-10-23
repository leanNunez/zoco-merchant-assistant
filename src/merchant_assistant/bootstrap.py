"""Composition root: the only place that wires adapters to ports. The evaluation reuses it."""

from __future__ import annotations

import openai

from merchant_assistant.knowledge.adapters.missing_key import MissingKeyEmbedder
from merchant_assistant.knowledge.adapters.openai_embedder import OpenAIEmbedder
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.embedding import CachedEmbedder
from merchant_assistant.knowledge.ports import Embedder
from merchant_assistant.shared.config import Settings


def build_embedder(settings: Settings) -> CachedEmbedder:
    """Embeddings through the on-disk cache; without a key, only cached texts work."""
    inner: Embedder
    if settings.openai_api_key is None:
        inner = MissingKeyEmbedder(settings.embeddings_model)
    else:
        client = openai.OpenAI(
            api_key=settings.openai_api_key.get_secret_value(), timeout=30, max_retries=2
        )
        inner = OpenAIEmbedder(client, settings.embeddings_model)
    return CachedEmbedder(inner, SqliteEmbeddingCache(settings.embeddings_cache_path))
