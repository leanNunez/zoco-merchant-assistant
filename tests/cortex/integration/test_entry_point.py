"""The contract's command, `python evaluation_adapter.py ...`, exactly as IT types it."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import evaluation_adapter
import pytest

from cortex.bootstrap import DEFAULT_TRACE_KEY
from cortex.observability.redaction import TraceRedactor

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


# --------------------------------------------------------------------------- the world directory


def _adapter(*arguments: str | Path) -> int:
    return evaluation_adapter.main([str(argument) for argument in arguments])


def _current_umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def test_a_workdir_that_already_holds_a_world_is_refused_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = tmp_path / "world"
    assert (
        _adapter(
            "--scenario",
            ROOT / "examples" / "T09.json",
            "--output",
            tmp_path / "a.json",
            "--workdir",
            world,
        )
        == 0
    )
    before = sorted(path.name for path in world.rglob("*"))
    capsys.readouterr()

    # Running T11 in T09's world used to report the claim T09 had filed.
    with pytest.raises(SystemExit) as refused:
        _adapter(
            "--scenario",
            ROOT / "examples" / "T11.json",
            "--output",
            tmp_path / "b.json",
            "--workdir",
            world,
        )

    stderr = capsys.readouterr().err
    assert refused.value.code == 2
    assert stderr.strip().splitlines()[-1].endswith("--workdir must be empty or not exist yet")
    assert not (tmp_path / "b.json").exists()
    assert sorted(path.name for path in world.rglob("*")) == before  # nothing was touched


def test_a_workdir_that_is_a_file_is_refused(tmp_path: Path) -> None:
    occupied = tmp_path / "world"
    occupied.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        _adapter(
            "--scenario",
            ROOT / "examples" / "T02.json",
            "--output",
            tmp_path / "o.json",
            "--workdir",
            occupied,
        )

    assert refused.value.code == 2


@pytest.mark.parametrize("create_first", [False, True])
def test_a_missing_or_empty_workdir_is_used(tmp_path: Path, create_first: bool) -> None:
    world = tmp_path / "world"
    if create_first:
        world.mkdir()

    code = _adapter(
        "--scenario",
        ROOT / "examples" / "T02.json",
        "--output",
        tmp_path / "o.json",
        "--workdir",
        world,
    )

    assert code == 0
    assert (world / "cortex.db").exists()


def test_the_runtime_files_are_private_and_the_umask_is_restored(tmp_path: Path) -> None:
    world = tmp_path / "world"
    earlier = os.umask(0o022)  # what a shell usually leaves: group and others may read
    try:
        code = _adapter(
            "--scenario",
            ROOT / "examples" / "T09.json",
            "--output",
            tmp_path / "o.json",
            "--workdir",
            world,
        )
        restored = _current_umask()
    finally:
        os.umask(earlier)

    assert code == 0
    entries = [world, *world.rglob("*")]
    assert any(path.suffix == ".db" for path in entries)
    assert [path.name for path in entries if path.stat().st_mode & 0o077] == []
    assert restored == 0o022  # main() ran in this process and must leak nothing


def test_the_umask_is_restored_when_the_scenario_is_refused(tmp_path: Path) -> None:
    scenario = tmp_path / "bad.json"
    scenario.write_text('{"surprise": true}', encoding="utf-8")
    earlier = os.umask(0o022)
    try:
        code = _adapter(
            "--scenario", scenario, "--output", tmp_path / "o.json", "--workdir", tmp_path / "world"
        )
        restored = _current_umask()
    finally:
        os.umask(earlier)

    assert (code, restored) == (2, 0o022)


# --------------------------------------------------------------------------- the trace key


def _traced_principal(output: Path) -> str:
    events = json.loads(output.read_text(encoding="utf-8"))["events"]
    return str(next(event["principal"] for event in events if event["type"] == "run_started"))


def _run_t02(tmp_path: Path, name: str) -> Path:
    output = tmp_path / name
    assert _adapter("--scenario", ROOT / "examples" / "T02.json", "--output", output) == 0
    return output


def test_without_a_trace_key_the_demo_default_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CORTEX_TRACE_KEY", raising=False)

    traced = _traced_principal(_run_t02(tmp_path, "o.json"))

    assert traced == TraceRedactor(DEFAULT_TRACE_KEY).principal("comercio_a", "operador_a")


def test_the_trace_key_of_the_environment_changes_the_pseudonym(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CORTEX_TRACE_KEY", raising=False)
    default = _traced_principal(_run_t02(tmp_path, "default.json"))
    monkeypatch.setenv("CORTEX_TRACE_KEY", "a-deployment-secret")

    keyed = _traced_principal(_run_t02(tmp_path, "keyed.json"))

    assert keyed == TraceRedactor("a-deployment-secret").principal("comercio_a", "operador_a")
    assert keyed != default


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_an_empty_or_blank_trace_key_is_refused_with_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setenv("CORTEX_TRACE_KEY", value)

    with pytest.raises(SystemExit) as refused:
        _adapter("--scenario", ROOT / "examples" / "T02.json", "--output", tmp_path / "o.json")

    assert refused.value.code == 2
    assert "CORTEX_TRACE_KEY" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


def test_a_scenario_with_a_lone_surrogate_exits_2_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (ROOT / "examples" / "T01.json").read_text(encoding="utf-8").rstrip()
    scenario = tmp_path / "surrogate.json"
    # The JSON escape is valid text; the string it decodes to cannot be encoded as UTF-8.
    scenario.write_text(text[:-1] + ', "message": "hola \\ud800"}', encoding="utf-8")
    output = tmp_path / "out.json"

    code = _adapter("--scenario", scenario, "--output", output)

    assert code == 2
    assert "UTF-8" in capsys.readouterr().err
    assert not output.exists()
