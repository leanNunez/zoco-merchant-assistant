"""The runner: fresh worlds, valid and invalid runs, and the limits of a live run.

The live mode is exercised only with fakes shaped like the chat-model port: no network, no key.
"""

from collections.abc import Sequence
from decimal import Decimal

import pytest
from evaluation.conversations.checks import (
    complaint_rows,
    customer_saw,
    evidence,
    lock,
    tool_called,
    utility,
)
from evaluation.conversations.runner import LiveSetup, RunSettings, run_evaluation, select
from evaluation.conversations.scenario import Category, Say, Scenario, When
from evaluation.conversations.script import Policy, View

from merchant_assistant.agent.adapters.errors import ModelAuthError, ModelUnavailable
from merchant_assistant.agent.domain.messages import Message, ModelReply
from merchant_assistant.agent.domain.tools import ToolSpec
from merchant_assistant.agent.ports import Completion
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

A = {"motivo": "Entrada no emitida", "descripcion": "Pagué la entrada y no me llegó el correo."}


def proposer() -> Policy:
    def policy(view: View) -> ModelReply:
        return view.call(("crear_reclamo", A))

    return policy


def broken() -> Policy:
    def policy(view: View) -> ModelReply:
        raise KeyError("a bug in the script")

    return policy


YES = Say("sí", tag="yes", when=When.CONFIRMATION_REQUESTED)


def complaint_case(case_id: str = "S04", script: object = proposer, second: Say = YES) -> Scenario:
    return Scenario(
        id=case_id,
        title="Reclamo",
        category=Category.SUCCESS,
        steps=(Say("Quiero reclamar."), second),
        expected_behaviour="registra uno",
        tools_expected=("crear_reclamo",),
        tools_forbidden=(),
        checks=(
            lock(complaint_rows(1)),
            utility(customer_saw(r"REC-[0-9]{6}")),
            evidence(tool_called("crear_reclamo", outcome="proposed")),
        ),
        model_script=script,  # type: ignore[arg-type]
        script_note="propone",
    )


def test_scripted_runs_are_valid_scored_and_reproducible() -> None:
    evaluation = run_evaluation([complaint_case()], RunSettings("scripted", reps=2))

    assert [(r.rep, r.seed, r.valid, r.passed) for r in evaluation.results] == [
        (0, 0, True, True),
        (1, 1, True, True),
    ]
    first = evaluation.results[0]
    assert [c.kind for c in first.checks] == ["lock", "utility", "evidence"]
    assert len(first.scored) == 2
    assert first.observation is not None
    assert first.observation.rows[0].id == "REC-000001"
    assert first.observation.rows[0].conversation_id == "eval-s04-r0"
    assert (first.search_mode, first.cost_usd) == ("degraded", None)
    assert not evaluation.partial


def test_the_null_model_fails_what_needs_an_action() -> None:
    (result,) = run_evaluation([complaint_case()], RunSettings("null", reps=1)).results
    assert result.valid
    assert not result.passed
    assert result.failed_checks() == ("complaint_rows==1", r"customer_saw(REC-[0-9]{6})")


def test_a_broken_script_is_an_invalid_run_never_a_model_failure() -> None:
    (result,) = run_evaluation(
        [complaint_case(script=broken)], RunSettings("scripted", reps=1)
    ).results
    assert (result.valid, result.failure_class, result.checks) == (False, "harness_exception", ())


def test_only_selects_known_cases() -> None:
    cases = [complaint_case("S01"), complaint_case("S02")]
    assert [c.id for c in select(cases, ["S02"])] == ["S02"]
    with pytest.raises(ValueError, match="S99"):
        select(cases, ["S99"])


# --------------------------------------------------------------------------- live, with fakes


class FakeLive:
    """Shaped like the chat-model port; proposes the complaint and reports usage."""

    def __init__(
        self, served: str = "gpt-4o-mini-2024-07-18", fail: Exception | None = None
    ) -> None:
        self.served = served
        self.fail = fail
        self.calls = 0

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        reply = View(tuple(messages), (), lambda: f"call-{self.calls}").call(("crear_reclamo", A))
        return Completion(reply, self.served, 100_000, 10_000)  # 0.021 USD per call


def live(model: FakeLive, ticks: list[float] | None = None) -> LiveSetup:
    """A live setup over a fake model; the timer plays `ticks`, then stays at the last one."""
    remaining = list(ticks or [0.0])

    def timer() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return LiveSetup(model, "gpt-4o-mini", Settings(_env_file=None, openai_api_key=None), timer)


def test_a_live_run_records_cost_and_stops_at_the_cap_as_partial() -> None:
    cases = [complaint_case("S01"), complaint_case("S02")]
    settings = RunSettings("live", reps=3, budget_usd=Decimal("0.05"))

    evaluation = run_evaluation(cases, settings, live=live(FakeLive()))

    # Repetition-major: S01 r0, S02 r0, S01 r1 (0.063 spent), then S02 r1 finds the cap reached.
    assert [(r.scenario_id, r.rep, r.valid) for r in evaluation.results] == [
        ("S01", 0, True),
        ("S01", 1, True),
        ("S02", 0, True),
        ("S02", 1, False),
    ]
    assert evaluation.results[0].cost_usd == Decimal("0.021")
    assert evaluation.results[3].failure_class == "budget_exhausted"
    assert evaluation.stop_reason == "budget_exhausted"
    assert evaluation.not_run == (("S01", 2), ("S02", 2))
    assert evaluation.partial
    assert evaluation.spent_usd == Decimal("0.063")
    served = evaluation.results[0].observation
    assert served is not None
    assert served.model_calls[0].served_model == "gpt-4o-mini-2024-07-18"


def test_an_unavailable_provider_makes_the_run_invalid_and_stops_that_conversation() -> None:
    model = FakeLive(fail=ModelUnavailable("503"))
    (result,) = run_evaluation(
        [complaint_case()], RunSettings("live", reps=1), live=live(model)
    ).results

    assert (result.valid, result.failure_class, result.checks) == (
        False,
        "provider_unavailable",
        (),
    )
    assert model.calls == 1  # the second customer message was never sent


def test_refused_credentials_stop_the_whole_run() -> None:
    evaluation = run_evaluation(
        [complaint_case("S01"), complaint_case("S02")],
        RunSettings("live", reps=2),
        live=live(FakeLive(fail=ModelAuthError("401"))),
    )
    assert [r.failure_class for r in evaluation.results] == ["authentication"]
    assert evaluation.stop_reason == "authentication"
    assert len(evaluation.not_run) == 3


def test_a_conversation_over_its_wall_clock_ceiling_is_invalid() -> None:
    # The conversation starts, call 1 is checked, timed (0 to 1 s), then the check before call 2.
    ticks = [0.0, 0.0, 0.0, 1.0, 500.0]
    settings = RunSettings("live", reps=1, conversation_seconds=60.0)

    case = complaint_case(second=Say("Mejor otra cosa."))  # the model is called twice

    (result,) = run_evaluation([case], settings, live=live(FakeLive(), ticks)).results

    assert (result.valid, result.failure_class) == (False, "timeout")


def test_the_live_mode_needs_its_setup_and_only_it() -> None:
    with pytest.raises(ValueError, match="LiveSetup"):
        run_evaluation([complaint_case()], RunSettings("live"))
    with pytest.raises(ValueError, match="LiveSetup"):
        run_evaluation([complaint_case()], RunSettings("scripted"), live=live(FakeLive()))
    with pytest.raises(ValueError, match="reps"):
        RunSettings("scripted", reps=0)
