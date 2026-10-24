"""The command line: reproducible files, and friendly one-line refusals before any spending.

The live mode is exercised with a fake shaped like the chat-model port: no key, no network.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest
from evaluation.conversations.__main__ import Dependencies, main
from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.script import View

from merchant_assistant.agent.adapters.errors import ModelAuthError
from merchant_assistant.agent.domain.messages import Message
from merchant_assistant.agent.domain.tools import ToolSpec
from merchant_assistant.agent.ports import ChatModel, Completion
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

FILES = ("results.json", "errors.jsonl", "report.md")


class FakeLive:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        view = View(tuple(messages), tuple(t.name for t in tools), lambda: f"call-{self.calls}")
        arguments = {"motivo": "Entrada no emitida", "descripcion": "Pagué y no me llegó nada."}
        return Completion(
            view.call(("crear_reclamo", arguments)), "gpt-4o-mini-2024-07-18", 1000, 100
        )


def live_deps(
    settings: Settings, model: ChatModel | None = None, budget: str | None = None
) -> Dependencies:
    return Dependencies(
        load_settings=lambda: settings,
        build_model=lambda _: model or FakeLive(),
        scenarios=SCENARIOS,
        environment=lambda name: budget if name == "EVAL_BUDGET_USD" else None,
    )


def keyed(**changes: object) -> Settings:
    return Settings(_env_file=None, openai_api_key=None, llm_api_key="sk-test-only", **changes)  # type: ignore[arg-type]


def test_two_scripted_runs_write_byte_identical_files(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"

    assert main(["run", "--mode", "scripted", "--reps", "1", "--out", str(first)]) == 0
    assert main(["run", "--mode", "scripted", "--reps", "1", "--out", str(second)]) == 0

    for name in FILES:
        content = (first / name).read_bytes()
        assert content == (second / name).read_bytes(), name
        assert b"eval-conv-" not in content
        assert str(tmp_path).encode() not in content
    assert "## Validación del evaluador" in (first / "report.md").read_text("utf-8")


def test_the_live_mode_without_a_key_says_so_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = live_deps(Settings(_env_file=None, openai_api_key=None, llm_api_key=None))

    assert main(["run", "--mode", "live", "--out", str(tmp_path)], deps=deps) == 2

    error = capsys.readouterr().err
    assert "Falta la clave del modelo" in error
    assert "Traceback" not in error
    assert error.count("\n") == 1
    assert not (tmp_path / "results.json").exists()


@pytest.mark.parametrize(
    ("argv", "settings", "budget", "message"),
    [
        (["--budget-usd", "abc"], keyed(), None, "tope de gasto"),
        ([], keyed(), "0", "tope de gasto"),
        ([], keyed(llm_model="mystery-model"), None, "No hay precio conocido"),
        (["--only", "S99"], keyed(), None, "Escenarios desconocidos: S99"),
        (["--reps", "0"], keyed(), None, "--reps"),
    ],
)
def test_bad_requests_are_refused_before_anything_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    settings: Settings,
    budget: str | None,
    message: str,
) -> None:
    model = FakeLive()
    deps = live_deps(settings, model, budget)

    assert main(["run", "--mode", "live", "--out", str(tmp_path), *argv], deps=deps) == 2

    assert message in capsys.readouterr().err
    assert model.calls == 0


def test_a_live_run_with_a_fake_model_records_cost_and_the_served_model(tmp_path: Path) -> None:
    deps = live_deps(keyed())

    code = main(
        ["run", "--mode", "live", "--only", "S04", "--reps", "2", "--out", str(tmp_path)], deps=deps
    )

    assert code == 0
    report = (tmp_path / "report.md").read_text("utf-8")
    assert "`gpt-4o-mini-2024-07-18`" in report
    assert "Gastado | USD 0.00042" in report
    assert "corridas válidas: 2; corridas inválidas (no puntúan): 0" in report
    assert "Costo: USD 0.0004" in report


def test_the_cap_stops_a_live_run_and_marks_it_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = live_deps(keyed(), budget="0.0002")

    code = main(["run", "--mode", "live", "--only", "S04,S12", "--out", str(tmp_path)], deps=deps)

    assert code == 0
    assert "PARCIALES" in capsys.readouterr().err
    assert "**Resultados PARCIALES**" in (tmp_path / "report.md").read_text("utf-8")
    assert "budget_exhausted" in (tmp_path / "errors.jsonl").read_text("utf-8")


def test_refused_credentials_end_the_run_with_a_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = live_deps(keyed(), FakeLive(fail=ModelAuthError("401")))

    assert main(["run", "--mode", "live", "--only", "S01", "--out", str(tmp_path)], deps=deps) == 1

    assert "rechazó las credenciales" in capsys.readouterr().err
    assert "authentication" in (tmp_path / "errors.jsonl").read_text("utf-8")


def test_the_split_chooses_the_scenarios_and_a_held_out_run_has_its_own_directory(
    tmp_path: Path,
) -> None:
    deps = live_deps(keyed())

    dev = main(
        ["run", "--mode", "live", "--split", "dev", "--only", "S04", "--reps", "1"],
        results_dir=tmp_path,
        deps=deps,
    )
    held = main(
        ["run", "--mode", "live", "--split", "heldout", "--only", "H01", "--reps", "1"],
        results_dir=tmp_path,
        deps=deps,
    )

    assert (dev, held) == (0, 0)
    assert (tmp_path / "live" / "report.md").exists()  # dev keeps the directory of the mode
    held_report = (tmp_path / "live-heldout" / "report.md").read_text("utf-8")
    assert "| Partición de escenarios | `heldout`" in held_report
    assert "[H01](#h01)" in held_report
    assert "[S04]" not in held_report
    dev_report = (tmp_path / "live" / "report.md").read_text("utf-8")
    assert "Partición `dev`: son los escenarios con los que se ajustaron" in dev_report


def test_a_scenario_of_the_other_split_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = live_deps(keyed())
    argv = ["run", "--mode", "live", "--out", str(tmp_path), "--split", "dev", "--only", "H01"]

    assert main(argv, deps=deps) == 2

    assert "Escenarios desconocidos: H01" in capsys.readouterr().err


def test_the_default_split_runs_both_and_the_report_compares_them(tmp_path: Path) -> None:
    assert main(["run", "--mode", "null", "--reps", "1", "--out", str(tmp_path)]) == 0

    report = (tmp_path / "report.md").read_text("utf-8")
    assert "### Por partición" in report
    assert "| `dev` | 26 |" in report
    assert "| `heldout` | 8 |" in report
