from pathlib import Path

import numpy as np
import pytest

from merchant_assistant.bootstrap import build_embedder
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.embedding import embedding_key
from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration


def offline_settings(path: Path) -> Settings:
    return Settings(_env_file=None, openai_api_key=None, embeddings_cache_path=path)


def test_without_a_key_cached_texts_are_served_offline(tmp_path: Path) -> None:
    path = tmp_path / "e.sqlite"
    key = embedding_key("text-embedding-3-small", "hola")
    SqliteEmbeddingCache(path).put_many({key: np.array([1.0, 2.0])}, model="m")

    embedder = build_embedder(offline_settings(path))

    assert embedder.embed(["hola"]).tolist() == [[1.0, 2.0]]
    assert embedder.inner_calls == 0


def test_without_a_key_an_uncached_text_fails_with_the_fix_in_the_detail(
    tmp_path: Path,
) -> None:
    embedder = build_embedder(offline_settings(tmp_path / "e.sqlite"))

    with pytest.raises(EmbeddingsUnavailableError) as refused:
        embedder.embed(["hola"])

    assert "set OPENAI_API_KEY in .env" in refused.value.detail
