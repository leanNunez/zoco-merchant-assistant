"""The committed results are what the code produces now: no stale numbers in the repository.

If this fails, the code or the scenarios changed: run `make eval-agent` and commit the files.
"""

from pathlib import Path

import pytest
from evaluation.conversations.__main__ import RESULTS, main

pytestmark = pytest.mark.integration

FILES = ("results.json", "errors.jsonl", "report.md")


@pytest.mark.parametrize("mode", ["scripted", "null", "naive"])
def test_the_committed_results_match_a_fresh_run(tmp_path: Path, mode: str) -> None:
    assert main(["run", "--mode", mode, "--out", str(tmp_path)]) == 0
    for name in FILES:
        committed = (RESULTS / mode / name).read_bytes()
        assert (tmp_path / name).read_bytes() == committed, f"{mode}/{name} is stale"
