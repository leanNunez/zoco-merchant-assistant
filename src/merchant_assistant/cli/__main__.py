"""`python -m merchant_assistant.cli` (`make chat`): the console wired to the real terminal."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from merchant_assistant.agent.adapters.errors import LLMNotConfigured
from merchant_assistant.bootstrap import ConsoleRuntime, build_console_runtime
from merchant_assistant.cli.console import Console
from merchant_assistant.cli.logs import DEFAULT_LOG_PATH, file_logging
from merchant_assistant.cli.text import REASONS, UNEXPECTED
from merchant_assistant.shared.config import Settings

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2

_LOGGER = logging.getLogger(__name__)


def summary(runtime: ConsoleRuntime) -> str:
    """One line about what is running. It names the model and the retrieval mode, no secret."""
    mode = "híbrida" if runtime.search_mode == "hybrid" else "degradada (solo BM25)"
    reason = REASONS.get(runtime.search_reason, runtime.search_reason)
    return f"Modelo: {runtime.llm_label} | Recuperación: {mode}, {reason}"


_SECRET_FIELDS = frozenset({"openai_api_key", "llm_api_key", "operator_pin", "supervisor_pin"})


def invalid_settings(invalid: ValidationError) -> str:
    """What to fix, field by field. The text of a validation error is not printed as it is: it
    quotes the offending input, which for a secret field would be the secret."""
    problems = []
    for error in invalid.errors():
        field = ".".join(str(part) for part in error["loc"])
        name = field.upper() or "la ventana de atención"
        reason = (
            "" if field in _SECRET_FIELDS else f": {error['msg'].removeprefix('Value error, ')}"
        )
        problems.append(f"{name}{reason}")
    return "La configuración no es válida (revisá .env o el entorno).\n  " + "\n  ".join(problems)


def main(
    settings: Settings | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    log_path: Path = DEFAULT_LOG_PATH,
) -> int:
    """Open the console. The exit status is 0 after a normal session, non-zero when it could
    not start; in no case does a traceback or an exception message reach the terminal."""
    with file_logging(log_path):
        try:
            loaded = Settings() if settings is None else settings
        except ValidationError as invalid:
            # Names only: the message of a validation error quotes the offending value.
            fields = sorted(
                {".".join(str(part) for part in e["loc"]).upper() for e in invalid.errors()}
            )
            output_fn(
                "La configuración no es válida. Revisá en .env o en el entorno: "
                f"{', '.join(fields)}."
            )
            return EXIT_FAILED
        try:
            runtime = build_console_runtime(loaded)
        except LLMNotConfigured as missing:
            output_fn(missing.user_message)
            return EXIT_NOT_CONFIGURED
        except Exception:
            _LOGGER.exception("the console could not start")
            output_fn(f"{UNEXPECTED} La consola no pudo iniciar; el detalle está en {log_path}.")
            return EXIT_FAILED
        output_fn(summary(runtime))
    try:
        Console(runtime, input_fn, output_fn, log_path=log_path).run()
    except Exception:
        # Outside the command boundary (a broken terminal, say): still no traceback on screen.
        with file_logging(log_path):
            _LOGGER.exception("the console stopped unexpectedly")
        with suppress(Exception):
            output_fn(UNEXPECTED)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
