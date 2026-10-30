"""The contract's command, `python evaluation_adapter.py ...`, exactly as IT types it."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import evaluation_adapter
import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SYSTEM_PYTHON = Path("/usr/bin/python3")


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="no system python3 on this machine")
def test_the_literal_command_works_with_the_system_python_from_another_directory(
    tmp_path: Path,
) -> None:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    output = tmp_path / "resultado.json"

    done = subprocess.run(  # noqa: S603 - our own adapter, fixed arguments
        [
            str(SYSTEM_PYTHON),
            str(ROOT / "evaluation_adapter.py"),
            "--scenario",
            str(ROOT / "examples" / "T02.json"),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["executed_tools"] == ["consultar_ventas"]


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="no system python3 on this machine")
def test_without_the_project_environment_the_adapter_says_how_to_set_it_up(tmp_path: Path) -> None:
    # A copy of the adapter with no `.venv` next to it: nothing to re-run with.
    shutil.copy(ROOT / "evaluation_adapter.py", tmp_path / "evaluation_adapter.py")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    done = subprocess.run(  # noqa: S603 - our own adapter, fixed arguments
        [str(SYSTEM_PYTHON), "evaluation_adapter.py", "--scenario", "x.json", "--output", "y.json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 2
    assert done.stderr.strip().splitlines() == [
        "the harness is not installed: run `make setup` first, or use `uv run`"
    ]


def test_a_configuration_that_loosens_a_policy_exits_2_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(ROOT / "kit", tmp_path / "kit")
    config_path = tmp_path / "kit" / "harness_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["policies"]["log_secrets"] = True
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(evaluation_adapter, "ROOT", tmp_path)
    output = tmp_path / "resultado.json"

    code = evaluation_adapter.main(
        ["--scenario", str(ROOT / "examples" / "T02.json"), "--output", str(output)]
    )

    stderr = capsys.readouterr().err
    assert code == 2
    assert len(stderr.strip().splitlines()) == 1
    assert "log_secrets" in stderr
    assert not output.exists()
