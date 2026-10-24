"""CLI: `python -m evaluation.conversations run --mode scripted|null|naive|live [--split ...]`.

Writes `results.json`, `errors.jsonl` and `report.md` under
`evaluation/results/conversations/<mode>/`. The scripted run also runs the null and naive modes
in memory, to write the "Validación del evaluador" section of its report.

Messages for the person running it are Spanish and one line, and never a traceback. Provider
details are never printed: an SDK message can quote part of the API key. The live mode needs a
key, refuses a model it cannot price, and stops at its spending cap (`--budget-usd`, or
`EVAL_BUDGET_USD`, 1.00 by default).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.metrics import summarize
from evaluation.conversations.pricing import price_for
from evaluation.conversations.report import errors_jsonl, provenance, to_json, to_markdown
from evaluation.conversations.runner import (
    DEFAULT_BUDGET_USD,
    DEFAULT_REPS,
    MODES,
    Evaluation,
    LiveSetup,
    Mode,
    RunSettings,
    run_evaluation,
)
from evaluation.conversations.scenario import SPLITS, Scenario, SplitChoice, in_split
from evaluation.conversations.validation import Validation, validate
from merchant_assistant.agent.ports import ChatModel
from merchant_assistant.bootstrap import build_chat_model
from merchant_assistant.shared.config import Settings
from merchant_assistant.shared.errors import DomainError

REPOSITORY = Path(__file__).resolve().parents[2]
RESULTS = REPOSITORY / "evaluation" / "results" / "conversations"
BUDGET_VARIABLE = "EVAL_BUDGET_USD"
STOP_MESSAGES = {
    "authentication": "El proveedor rechazó las credenciales (authentication): revisá la clave.",
    "model_not_found": "El proveedor no conoce el modelo configurado (model_not_found).",
    "unknown_cost": "Un modelo servido no tiene precio conocido: la corrida se detuvo para no "
    "gastar sin tope (agregalo en evaluation/conversations/pricing.py).",
}


@dataclass(frozen=True)
class Dependencies:
    """What the CLI takes from the outside, so tests can run it without a key or a network."""

    load_settings: Callable[[], Settings] = Settings
    build_model: Callable[[Settings], ChatModel] = build_chat_model
    scenarios: Sequence[Scenario] = SCENARIOS
    environment: Callable[[str], str | None] = os.environ.get


def main(
    argv: list[str] | None = None,
    *,
    results_dir: Path = RESULTS,
    deps: Dependencies | None = None,
) -> int:
    deps = deps or Dependencies()
    parser = argparse.ArgumentParser(prog="python -m evaluation.conversations")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="corre los escenarios y guarda los resultados")
    run.add_argument("--mode", choices=MODES, required=True)
    run.add_argument("--reps", type=int, default=DEFAULT_REPS)
    run.add_argument("--only", default="", help="ids separados por coma, por ejemplo S01,X2")
    run.add_argument(
        "--split",
        choices=SPLITS,
        default="all",
        help="dev: los escenarios con los que se ajusta el agente; heldout: los reservados "
        "para medir sin sesgo; all: ambos (por defecto)",
    )
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--budget-usd", default=None)
    run.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    mode: Mode = args.mode
    only = tuple(item.strip() for item in args.only.split(",") if item.strip())
    split: SplitChoice = args.split
    known = {scenario.id for scenario in in_split(deps.scenarios, split)}
    unknown = sorted(set(only) - known)
    if unknown:
        print(f"Escenarios desconocidos: {', '.join(unknown)}.", file=sys.stderr)
        return 2
    if args.reps < 1:
        print("--reps tiene que ser 1 o más.", file=sys.stderr)
        return 2
    budget = _budget(args.budget_usd, deps)
    if budget is None:
        return 2
    settings = RunSettings(
        mode, reps=args.reps, base_seed=args.seed, only=only, split=split, budget_usd=budget
    )
    live = _live(deps) if mode == "live" else None
    if mode == "live" and live is None:
        return 2
    evaluation = run_evaluation(deps.scenarios, settings, live=live)
    validation = _validation(evaluation, deps.scenarios) if mode == "scripted" else None
    # A held-out run never overwrites the results the dev iterations are compared against.
    out = args.out or results_dir / (f"{mode}-heldout" if split == "heldout" else mode)
    write(out, evaluation, validation)
    return _report(out, evaluation)


def write(out: Path, evaluation: Evaluation, validation: Validation | None) -> None:
    summary = summarize(evaluation)
    source = provenance(evaluation)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        to_json(evaluation, summary, source, validation), encoding="utf-8"
    )
    (out / "errors.jsonl").write_text(errors_jsonl(evaluation), encoding="utf-8")
    (out / "report.md").write_text(
        to_markdown(evaluation, summary, source, validation), encoding="utf-8"
    )


def _validation(scripted: Evaluation, scenarios: Sequence[Scenario]) -> Validation:
    """The same scenarios, repetitions and seeds with the null model and the naive agent."""
    base = scripted.settings

    def again(mode: Mode) -> Evaluation:
        settings = RunSettings(
            mode, reps=base.reps, base_seed=base.base_seed, only=base.only, split=base.split
        )
        return run_evaluation(scenarios, settings)

    return validate(scripted, again("null"), again("naive"))


def _budget(raw: str | None, deps: Dependencies) -> Decimal | None:
    text = raw if raw is not None else deps.environment(BUDGET_VARIABLE)
    if text is None or not text.strip():
        return DEFAULT_BUDGET_USD
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        value = Decimal(0)
    if not value.is_finite() or value <= 0:
        print(
            f"El tope de gasto tiene que ser un número mayor que 0 (recibido: {text!r}).",
            file=sys.stderr,
        )
        return None
    return value


def _live(deps: Dependencies) -> LiveSetup | None:
    try:
        settings = deps.load_settings()
    except ValidationError as error:
        first = error.errors()[0]
        print(f"Configuración inválida en {first['loc'][0]}: {first['msg']}", file=sys.stderr)
        return None
    if settings.effective_llm_api_key() is None:
        print(
            "Falta la clave del modelo: definí LLM_API_KEY (u OPENAI_API_KEY si usás OpenAI sin "
            "LLM_BASE_URL) en .env para correr la evaluación con el modelo real.",
            file=sys.stderr,
        )
        return None
    if price_for(settings.llm_model) is None:
        print(
            f"No hay precio conocido para el modelo {settings.llm_model!r}: agregalo en "
            "evaluation/conversations/pricing.py para poder respetar el tope de gasto.",
            file=sys.stderr,
        )
        return None
    try:
        model = deps.build_model(settings)
    except DomainError as error:
        print(f"No se pudo preparar el modelo ({error.code}).", file=sys.stderr)
        return None
    return LiveSetup(model, settings.llm_model, settings)


def _report(out: Path, evaluation: Evaluation) -> int:
    summary = summarize(evaluation)
    compliance = summary.overall.compliance
    print(
        f"Resultados en {out}. Cumplimiento: {compliance.passes}/{compliance.total} corridas "
        f"válidas; inválidas: {summary.invalid}."
    )
    stop = evaluation.stop_reason
    if stop in STOP_MESSAGES:
        print(STOP_MESSAGES[stop], file=sys.stderr)
        return 1
    if evaluation.partial:
        print(
            f"Aviso: resultados PARCIALES ({stop or 'corridas sin ejecutar'}); "
            f"{len(evaluation.not_run)} corrida(s) no se ejecutaron.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
