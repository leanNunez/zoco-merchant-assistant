"""The cached embedder, checked with an in-memory cache and a counting fake provider."""

from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from merchant_assistant.knowledge.embedding import CachedEmbedder, embedding_key
from merchant_assistant.knowledge.errors import InvalidEmbeddingError


class LengthEmbedder:
    """Maps a text to (len(text), 1): easy to predict, and it records every call."""

    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
        self.calls.append(list(texts))
        return np.array([[float(len(text)), 1.0] for text in texts])


class DictCache:
    def __init__(self) -> None:
        self.vectors: dict[str, NDArray[np.float64]] = {}
        self.models: dict[str, str] = {}

    def get_many(self, keys: Sequence[str]) -> dict[str, NDArray[np.float64]]:
        return {key: self.vectors[key] for key in keys if key in self.vectors}

    def put_many(self, items: Mapping[str, NDArray[np.float64]], *, model: str) -> None:
        for key, vector in items.items():
            self.vectors.setdefault(key, vector)
            self.models.setdefault(key, model)


class HalfPrecisionCache(DictCache):
    """Stores with less precision, as the SQLite cache does with float32."""

    def put_many(self, items: Mapping[str, NDArray[np.float64]], *, model: str) -> None:
        super().put_many(
            {k: v.astype(np.float16).astype(np.float64) for k, v in items.items()}, model=model
        )


# --- the key -----------------------------------------------------------------------------


def test_the_key_is_the_sha256_of_the_model_and_the_text_joined_by_a_newline() -> None:
    # sha256(b"m\nhola"), computed with `printf 'm\nhola' | sha256sum`
    assert embedding_key("m", "hola") == (
        "0204128b13accbc18f6ed6415f32d680f66115575f8706d7a06697c92e2948e6"
    )


def test_the_same_text_with_another_model_has_another_key() -> None:
    assert embedding_key("text-embedding-3-small", "hola") != embedding_key(
        "text-embedding-3-large", "hola"
    )


def test_the_text_is_not_normalized_so_a_changed_space_or_case_changes_the_key() -> None:
    keys = {embedding_key("m", text) for text in ("hola", "hola ", "Hola")}

    assert len(keys) == 3


# --- the cached embedder -----------------------------------------------------------------


def test_a_cold_cache_calls_the_provider_once_with_each_distinct_text() -> None:
    inner = LengthEmbedder()
    embedder = CachedEmbedder(inner, DictCache())

    vectors = embedder.embed(["ab", "abcd", "ab"])

    assert inner.calls == [["ab", "abcd"]]
    assert vectors.tolist() == [[2.0, 1.0], [4.0, 1.0], [2.0, 1.0]]
    assert (embedder.hits, embedder.misses, embedder.inner_calls) == (0, 2, 1)


def test_a_second_run_over_the_same_texts_does_not_call_the_provider() -> None:
    inner = LengthEmbedder()
    cache = DictCache()
    CachedEmbedder(inner, cache).embed(["ab", "abcd"])

    second = CachedEmbedder(inner, cache)
    vectors = second.embed(["abcd", "ab"])

    assert inner.calls == [["ab", "abcd"]]  # only the first run's call
    assert vectors.tolist() == [[4.0, 1.0], [2.0, 1.0]]
    assert (second.hits, second.misses, second.inner_calls) == (2, 0, 0)


def test_only_the_texts_missing_from_the_cache_are_sent_to_the_provider() -> None:
    inner = LengthEmbedder()
    embedder = CachedEmbedder(inner, DictCache())
    embedder.embed(["ab"])

    vectors = embedder.embed(["abc", "ab", "abcde"])

    assert inner.calls == [["ab"], ["abc", "abcde"]]
    assert vectors.tolist() == [[3.0, 1.0], [2.0, 1.0], [5.0, 1.0]]
    assert (embedder.hits, embedder.misses, embedder.inner_calls) == (1, 3, 2)


def test_new_vectors_are_stored_under_their_key_with_the_model_that_made_them() -> None:
    cache = DictCache()

    CachedEmbedder(LengthEmbedder(), cache).embed(["abc"])

    key = embedding_key("fake-model", "abc")
    assert list(cache.vectors) == [key]
    assert cache.vectors[key].tolist() == [3.0, 1.0]
    assert cache.models == {key: "fake-model"}


def test_a_miss_returns_the_vector_as_the_cache_stored_it_so_hits_and_misses_agree() -> None:
    # 1/3 is not representable in float16: the stored value is 0.333251953125.
    class ThirdEmbedder(LengthEmbedder):
        def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
            return np.array([[1 / 3, 1.0] for _ in texts])

    cache = HalfPrecisionCache()
    first = CachedEmbedder(ThirdEmbedder(), cache).embed(["x"])
    second = CachedEmbedder(ThirdEmbedder(), cache).embed(["x"])

    assert first.tolist() == second.tolist() == [[0.333251953125, 1.0]]


def test_no_texts_give_an_empty_matrix_without_calling_the_provider() -> None:
    inner = LengthEmbedder()
    embedder = CachedEmbedder(inner, DictCache())

    assert embedder.embed([]).shape == (0, 0)
    assert inner.calls == []
    assert embedder.inner_calls == 0


def test_the_model_is_the_inner_embedders() -> None:
    assert CachedEmbedder(LengthEmbedder(), DictCache()).model == "fake-model"


def test_a_provider_that_returns_the_wrong_number_of_rows_is_refused() -> None:
    class ShortEmbedder(LengthEmbedder):
        def embed(self, texts: Sequence[str]) -> NDArray[np.float64]:
            return np.array([[1.0, 1.0]])

    with pytest.raises(InvalidEmbeddingError) as refused:
        CachedEmbedder(ShortEmbedder(), DictCache()).embed(["a", "b"])

    assert refused.value.detail == "asked for 2 vectors, got shape (1, 2)"


def test_cached_vectors_of_different_dimensions_are_refused() -> None:
    cache = DictCache()
    cache.vectors[embedding_key("fake-model", "a")] = np.array([1.0, 0.0])
    cache.vectors[embedding_key("fake-model", "b")] = np.array([1.0, 0.0, 0.0])

    with pytest.raises(InvalidEmbeddingError, match="different shapes"):
        CachedEmbedder(LengthEmbedder(), cache).embed(["a", "b"])


def test_a_cache_that_drops_what_it_was_given_is_detected() -> None:
    class ForgetfulCache(DictCache):
        def put_many(self, items: Mapping[str, NDArray[np.float64]], *, model: str) -> None:
            pass

    with pytest.raises(InvalidEmbeddingError) as refused:
        CachedEmbedder(LengthEmbedder(), ForgetfulCache()).embed(["a"])

    assert refused.value.detail == "the cache lost 1 vectors it just stored"
