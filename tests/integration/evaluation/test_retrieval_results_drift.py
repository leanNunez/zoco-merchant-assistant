"""The committed retrieval results are what the code produces from the committed cache.

If this fails, the corpus, the queries, the cache or the code changed: run `make eval-retrieval`
(and `make index` if texts were added) and commit the files.
"""

from pathlib import Path

import pytest
from evaluation.retrieval.__main__ import RESULTS, main

from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

CACHE = Path("data/cache/embeddings.sqlite")


def offline_settings() -> Settings:
    return Settings(_env_file=None, openai_api_key=None, embeddings_cache_path=CACHE)


@pytest.mark.parametrize(
    ("command", "files"),
    [
        ("run", ("metrics.json", "report.md")),
        ("calibrate", ("gate_calibration.json", "gate_calibration.md")),
    ],
)
def test_the_committed_results_match_a_fresh_offline_run(
    tmp_path: Path, command: str, files: tuple[str, ...]
) -> None:
    assert main([command], settings=offline_settings(), results_dir=tmp_path) == 0

    for name in files:
        assert (tmp_path / name).read_bytes() == (RESULTS / name).read_bytes(), f"{name} is stale"
