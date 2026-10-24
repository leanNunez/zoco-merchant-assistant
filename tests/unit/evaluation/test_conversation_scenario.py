"""A scenario refuses to be written in a way that could not be judged or reported."""

from datetime import datetime

import pytest
from evaluation.conversations.checks import (
    complaint_rows,
    customer_saw,
    evidence,
    lock,
    utility,
)
from evaluation.conversations.scenario import (
    Category,
    FaultPlan,
    MoveClock,
    Say,
    Scenario,
    Step,
    Team,
    TeamCommand,
)
from evaluation.conversations.script import Policy, View

from merchant_assistant.agent.domain.drafts import DraftKind
from merchant_assistant.agent.domain.messages import ModelReply
from merchant_assistant.shared.faults import FaultMode


def polite() -> Policy:
    def policy(view: View) -> ModelReply:
        return view.say("Hola.")

    return policy


def scenario(**changes: object) -> Scenario:
    fields: dict[str, object] = {
        "id": "S01",
        "title": "t",
        "category": Category.SUCCESS,
        "steps": (Say("hola", tag="hi"),),
        "expected_behaviour": "e",
        "tools_expected": (),
        "tools_forbidden": (),
        "checks": (lock(complaint_rows(0)),),
        "model_script": polite,
        "script_note": "n",
    }
    fields.update(changes)
    return Scenario(**fields)  # type: ignore[arg-type]


def test_a_well_formed_scenario_lists_its_customer_messages() -> None:
    steps: tuple[Step, ...] = (Say("hola"), Team(TeamCommand.TAKEOVER), Say("sí"))
    assert [say.text for say in scenario(steps=steps).user_messages] == ["hola", "sí"]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "case-1"},
        {"steps": (Team(TeamCommand.TAKEOVER),)},
        {"steps": (Say("a", tag="t"), Say("b", tag="t"))},
        {"steps": (Say("a", tag="Bad Tag"),)},
        {"checks": (lock(complaint_rows(0)), utility(complaint_rows(0)))},
        {"checks": (utility(customer_saw("x")),)},
        {"checks": (evidence(complaint_rows(0)), utility(customer_saw("x")))},
        {"known_limitation": "x"},
        {"known_limitation": "x", "expected_failures": ("not_a_check",)},
    ],
)
def test_a_scenario_that_could_not_be_judged_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        scenario(**changes)


def test_team_steps_carry_what_their_command_needs() -> None:
    with pytest.raises(ValueError, match="draft kind"):
        Team(TeamCommand.APPROVE)
    with pytest.raises(ValueError, match="draft kind"):
        Team(TeamCommand.TAKEOVER, draft_kind=DraftKind.REPLY)
    with pytest.raises(ValueError, match="text"):
        Team(TeamCommand.REPLY)


def test_the_clock_only_moves_to_an_aware_instant_and_plans_are_positive() -> None:
    with pytest.raises(ValueError, match="aware"):
        MoveClock(datetime(2026, 10, 23, 22, 0))  # noqa: DTZ001 - the naive value under test
    with pytest.raises(ValueError, match="first_calls"):
        FaultPlan(FaultMode.TIMEOUT, first_calls=0)
    assert FaultPlan(FaultMode.ERROR).describe() == "error en todas las llamadas"
