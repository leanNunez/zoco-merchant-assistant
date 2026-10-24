"""The CLI over the real kit, offline: a pre-filled cache stands in for the provider."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from evaluation.retrieval.__main__ import DOCS, QUESTIONS, index_texts, main
from evaluation.retrieval.pipeline import texts_to_index

from merchant_assistant.knowledge.adapters.filesystem import load_documents, load_questions
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.embedding import embedding_key
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

MODEL = "text-embedding-3-small"


def offline_settings(cache: Path) -> Settings:
    return Settings(
        _env_file=None, openai_api_key=None, embeddings_model=MODEL, embeddings_cache_path=cache
    )


def filled_cache(path: Path, texts: list[str] | None = None) -> Path:
    """Every text the evaluation needs, with a vector derived from the text's own hash."""
    texts = index_texts() if texts is None else texts
    vectors = {
        embedding_key(MODEL, text): np.frombuffer(
            hashlib.sha256(text.encode()).digest(), np.uint8
        ).astype(np.float64)
        + 1.0
        for text in texts
    }
    SqliteEmbeddingCache(path).put_many(vectors, model=MODEL)
    return path


def test_index_with_a_complete_cache_makes_no_api_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = offline_settings(filled_cache(tmp_path / "cache.sqlite"))

    assert main(["index"], settings=settings) == 0

    assert capsys.readouterr().out.rstrip().endswith("Embebidos ahora: 0. Llamadas a la API: 0.")


def test_two_runs_over_a_complete_cache_write_identical_files_offline(tmp_path: Path) -> None:
    settings = offline_settings(filled_cache(tmp_path / "cache.sqlite"))

    assert main(["run"], settings=settings, results_dir=tmp_path / "first") == 0
    assert main(["run"], settings=settings, results_dir=tmp_path / "second") == 0

    for name in ("metrics.json", "report.md"):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()
    metrics = json.loads((tmp_path / "first" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["provenance"]["api_calls"] == 0
    assert metrics["provenance"]["embeddings_model"] == MODEL
    assert [question["id"] for question in metrics["per_question"]] == [
        f"Q{n:02d}" for n in range(1, 21)
    ]


@pytest.mark.parametrize("command", ["index", "run"])
def test_without_a_key_and_with_an_incomplete_cache_the_cli_fails_in_one_spanish_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    settings = offline_settings(tmp_path / "empty.sqlite")

    assert main([command], settings=settings, results_dir=tmp_path / "results") == 1

    captured = capsys.readouterr()
    assert captured.err == (
        "Faltan embeddings en la caché y no hay OPENAI_API_KEY configurada: "
        "agregala en .env y corré `make index`.\n"
    )
    assert not (tmp_path / "results").exists()


def test_index_embeds_the_calibration_queries_too(tmp_path: Path) -> None:
    only_the_evaluation = texts_to_index(load_documents(DOCS), load_questions(QUESTIONS))
    settings = offline_settings(filled_cache(tmp_path / "cache.sqlite", only_the_evaluation))

    # Without a key the missing calibration queries cannot be embedded: `make index` needs them.
    assert main(["index"], settings=settings) == 1
    assert len(index_texts()) > len(only_the_evaluation)


def test_calibrating_over_a_complete_cache_writes_identical_files_offline(tmp_path: Path) -> None:
    settings = offline_settings(filled_cache(tmp_path / "cache.sqlite"))

    assert main(["calibrate"], settings=settings, results_dir=tmp_path / "first") == 0
    assert main(["calibrate"], settings=settings, results_dir=tmp_path / "second") == 0

    for name in ("gate_calibration.json", "gate_calibration.md"):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()
    result = json.loads((tmp_path / "first" / "gate_calibration.json").read_text(encoding="utf-8"))
    assert result["provenance"]["api_calls"] == 0
    assert result["provenance"]["queries"] == {"kit": 20, "short_legit": 30, "off_topic": 45}
    assert result["provenance"]["chunking"] == "document"


def test_calibrating_without_a_key_and_with_an_incomplete_cache_fails_in_one_spanish_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = offline_settings(tmp_path / "empty.sqlite")

    assert main(["calibrate"], settings=settings, results_dir=tmp_path / "results") == 1

    assert capsys.readouterr().err.startswith("Faltan embeddings en la caché")
    assert not (tmp_path / "results").exists()
