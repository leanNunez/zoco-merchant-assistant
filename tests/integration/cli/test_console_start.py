"""Starting the console: the banner, the PINs, the summary line and the ways it refuses to start."""

import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from merchant_assistant.agent.adapters.scripted_model import ScriptedChatModel
from merchant_assistant.bootstrap import build_console_runtime
from merchant_assistant.cli.__main__ import EXIT_FAILED, EXIT_NOT_CONFIGURED, EXIT_OK, main
from merchant_assistant.cli.console import Console
from merchant_assistant.cli.logs import file_logging
from merchant_assistant.cli.text import UNEXPECTED
from merchant_assistant.shared.clock import FixedClock
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

FAKE_LLM_KEY = "gsk-FAKE-start-key"
CONFIGURED_PIN = "905817"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The developer's shell and any .env in the working directory must not decide these tests.
    for name in (
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_TEMPERATURE",
        "OPERATOR_PIN",
        "SUPERVISOR_PIN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    sales = tmp_path / "ventas.json"
    sales.write_text(json.dumps({"currency": "ARS", "days": []}), encoding="utf-8")
    values: dict[str, object] = {
        "docs_dir": Path(__file__).parents[3] / "docs",
        "embeddings_cache_path": tmp_path / "cache" / "embeddings.sqlite",
        "sales_path": sales,
        "complaints_db_path": tmp_path / "var" / "reclamos.db",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def run_console(tmp_path: Path, **overrides: object) -> tuple[str, str, str]:
    """Open the console on a scripted model and quit at once; returns the screen, and the PINs."""
    runtime = build_console_runtime(
        settings(tmp_path, **overrides),
        model=ScriptedChatModel([]),
        clock=FixedClock(datetime(2026, 10, 23, 12, tzinfo=UTC)),
        sleeper=lambda _: None,
    )
    screen: list[str] = []
    console = Console(runtime, _quit, screen.append, log_path=tmp_path / "var" / "chat.log")
    console.run()
    return (
        "\n".join(screen),
        runtime.operator_pin.get_secret_value(),
        runtime.supervisor_pin.get_secret_value(),
    )


def _quit(prompt: str) -> str:
    return "/quit"


# --------------------------------------------------------------------------- the PINs


def test_without_configured_pins_random_ones_are_shown_once_in_a_demo_banner(
    tmp_path: Path,
) -> None:
    screen, operator, supervisor = run_console(tmp_path)

    assert re.fullmatch(r"[0-9]{6}", operator)
    assert re.fullmatch(r"[0-9]{6}", supervisor)
    assert operator != supervisor
    assert screen.count(operator) == 1
    assert screen.count(supervisor) == 1
    assert "DEMO" in screen
    assert "simulador" in screen
    assert "no autenticación de producción" in screen


def test_two_runs_do_not_share_their_generated_pins(tmp_path: Path) -> None:
    pins = {run_console(tmp_path)[1] for _ in range(6)}

    assert len(pins) > 1  # a fixed PIN would be one more thing to guess once


def test_configured_pins_are_never_printed(tmp_path: Path) -> None:
    screen, _, _ = run_console(
        tmp_path,
        operator_pin=SecretStr(CONFIGURED_PIN),
        supervisor_pin=SecretStr("118822"),
    )

    assert CONFIGURED_PIN not in screen
    assert "118822" not in screen
    assert "DEMO" not in screen


def test_only_the_generated_pin_is_printed_when_one_role_is_configured(tmp_path: Path) -> None:
    screen, operator, supervisor = run_console(tmp_path, operator_pin=SecretStr(CONFIGURED_PIN))

    assert operator == CONFIGURED_PIN
    assert CONFIGURED_PIN not in screen
    assert screen.count(supervisor) == 1
    assert "supervisor:" in screen
    assert "operator:" not in screen


def test_a_generated_pin_never_equals_a_configured_one(tmp_path: Path) -> None:
    for _ in range(30):
        _, operator, supervisor = run_console(tmp_path, operator_pin=SecretStr("000000"))
        assert supervisor != operator


def test_the_runtime_does_not_show_its_pins_in_a_repr(tmp_path: Path) -> None:
    runtime = build_console_runtime(
        settings(tmp_path, operator_pin=SecretStr(CONFIGURED_PIN)),
        model=ScriptedChatModel([]),
        sleeper=lambda _: None,
    )

    shown = repr(runtime)

    assert CONFIGURED_PIN not in shown
    assert runtime.supervisor_pin.get_secret_value() not in shown


# --------------------------------------------------------------------------- main


def test_without_a_key_it_says_so_in_spanish_exits_non_zero_and_asks_for_no_input(
    tmp_path: Path,
) -> None:
    screen: list[str] = []

    def never(prompt: str) -> str:
        raise AssertionError("the console must not open")

    status = main(
        settings(tmp_path), input_fn=never, output_fn=screen.append, log_path=tmp_path / "c.log"
    )

    shown = "\n".join(screen)
    assert status == EXIT_NOT_CONFIGURED != EXIT_OK
    assert "LLM_API_KEY" in shown
    assert "Traceback" not in shown
    assert "sk-" not in shown


def test_the_key_of_openai_is_not_enough_for_a_custom_endpoint(tmp_path: Path) -> None:
    screen: list[str] = []

    status = main(
        settings(
            tmp_path,
            openai_api_key=SecretStr("sk-FAKE-openai"),
            llm_base_url="https://api.groq.com/openai/v1",
        ),
        input_fn=_quit,
        output_fn=screen.append,
        log_path=tmp_path / "c.log",
    )

    assert status == EXIT_NOT_CONFIGURED
    assert "sk-FAKE-openai" not in "\n".join(screen)


def test_a_configured_run_prints_one_summary_line_without_a_secret_and_exits_zero(
    tmp_path: Path,
) -> None:
    screen: list[str] = []
    configured = settings(
        tmp_path,
        llm_api_key=SecretStr(FAKE_LLM_KEY),
        llm_provider="groq",
        llm_base_url="https://api.groq.com/openai/v1",
        llm_model="openai/gpt-oss-120b",
    )

    status = main(configured, input_fn=_quit, output_fn=screen.append, log_path=tmp_path / "c.log")

    shown = "\n".join(screen)
    assert status == EXIT_OK
    (summary,) = [line for line in screen if line.startswith("Modelo:")]
    assert summary == (
        "Modelo: groq:openai/gpt-oss-120b | Recuperación: degradada (solo BM25), "
        "sin OPENAI_API_KEY para embeber las consultas"
    )
    assert FAKE_LLM_KEY not in shown
    assert "groq.com" not in shown


def test_an_invalid_setting_names_the_variable_and_never_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TEMPERATURE", "99")
    monkeypatch.setenv("LLM_BASE_URL", "ftp://user:hunter2-FAKE@host")
    screen: list[str] = []

    status = main(None, input_fn=_quit, output_fn=screen.append, log_path=tmp_path / "c.log")

    shown = "\n".join(screen)
    assert status == EXIT_FAILED
    assert "LLM_TEMPERATURE" in shown
    assert "LLM_BASE_URL" in shown
    assert "hunter2-FAKE" not in shown
    assert "Traceback" not in shown


def test_a_failure_of_the_terminal_itself_is_one_generic_line_and_a_log_entry(
    tmp_path: Path,
) -> None:
    screen: list[str] = []
    log_path = tmp_path / "var" / "chat.log"

    def broken_terminal(prompt: str) -> str:
        raise RuntimeError(f"the terminal said {FAKE_LLM_KEY}")

    status = main(
        settings(tmp_path, llm_api_key=SecretStr(FAKE_LLM_KEY)),
        input_fn=broken_terminal,
        output_fn=screen.append,
        log_path=log_path,
    )

    shown = "\n".join(screen)
    assert status == EXIT_FAILED
    assert shown.endswith(UNEXPECTED)
    assert FAKE_LLM_KEY not in shown
    assert "Traceback" not in shown
    assert "the console stopped unexpectedly" in log_path.read_text(encoding="utf-8")


def test_an_unexpected_start_up_failure_is_one_generic_line_and_a_log_entry(
    tmp_path: Path,
) -> None:
    screen: list[str] = []
    log_path = tmp_path / "var" / "chat.log"
    broken = tmp_path / "docs-is-a-file"
    broken.write_text("not a directory", encoding="utf-8")

    status = main(
        settings(tmp_path, llm_api_key=SecretStr(FAKE_LLM_KEY), docs_dir=broken),
        input_fn=_quit,
        output_fn=screen.append,
        log_path=log_path,
    )

    shown = "\n".join(screen)
    assert status == EXIT_FAILED
    assert "Ocurrió un error inesperado" in shown
    assert str(log_path) in shown
    assert "Traceback" not in shown
    assert FAKE_LLM_KEY not in shown
    assert "Traceback" in log_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the log file


def test_the_log_file_is_created_private_and_the_handler_is_removed_afterwards(
    tmp_path: Path,
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    path = tmp_path / "deep" / "var" / "chat.log"

    with file_logging(path):
        logging.getLogger("merchant_assistant.test").warning("something")
        logging.getLogger("merchant_assistant.test").info("too quiet")

    assert root.handlers == before
    text = path.read_text(encoding="utf-8")
    assert "something" in text
    assert "too quiet" not in text
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
