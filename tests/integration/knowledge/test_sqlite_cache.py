from pathlib import Path

import numpy as np
import pytest

from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.errors import InvalidEmbeddingError

pytestmark = pytest.mark.integration


def test_a_stored_vector_comes_back_as_float64(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")

    cache.put_many({"k1": np.array([0.5, -1.0, 2.0])}, model="m")
    found = cache.get_many(["k1"])

    assert list(found) == ["k1"]
    assert found["k1"].dtype == np.float64
    assert found["k1"].tolist() == [0.5, -1.0, 2.0]  # exact in float32 too


def test_absent_keys_are_left_out_of_the_result(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")
    cache.put_many({"k1": np.array([1.0, 2.0])}, model="m")

    assert list(cache.get_many(["nope", "k1", "also-nope"])) == ["k1"]


def test_vectors_survive_a_new_instance_over_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "e.sqlite"
    SqliteEmbeddingCache(path).put_many(
        {"k1": np.array([1.0, 2.0]), "k2": np.array([3.0, 4.0])}, model="m"
    )

    found = SqliteEmbeddingCache(path).get_many(["k1", "k2"])

    assert {key: vector.tolist() for key, vector in found.items()} == {
        "k1": [1.0, 2.0],
        "k2": [3.0, 4.0],
    }


def test_the_dimension_of_each_vector_is_preserved(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")

    cache.put_many({"short": np.ones(3), "long": np.ones(1536)}, model="m")
    found = cache.get_many(["short", "long"])

    assert (found["short"].shape, found["long"].shape) == ((3,), (1536,))


def test_values_are_stored_with_float32_precision(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")

    cache.put_many({"k": np.array([1 / 3])}, model="m")

    # float32(1/3) = 0.3333333432674408 (24-bit mantissa), not float64's 0.3333333333333333
    assert cache.get_many(["k"])["k"].tolist() == [0.3333333432674408]


def test_storing_a_key_again_keeps_the_first_vector(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")
    cache.put_many({"k": np.array([1.0, 0.0])}, model="m")

    cache.put_many({"k": np.array([0.0, 1.0])}, model="m")

    assert cache.get_many(["k"])["k"].tolist() == [1.0, 0.0]


def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "e.sqlite"

    SqliteEmbeddingCache(path).put_many({"k": np.array([1.0])}, model="m")

    assert path.is_file()


def test_a_batch_with_an_unusable_vector_stores_nothing(tmp_path: Path) -> None:
    cache = SqliteEmbeddingCache(tmp_path / "e.sqlite")

    with pytest.raises(InvalidEmbeddingError):
        cache.put_many({"good": np.array([1.0]), "bad": np.array([float("nan")])}, model="m")

    assert cache.get_many(["good", "bad"]) == {}
