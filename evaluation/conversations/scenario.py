"""A conversation scenario: what the simulated people do, what must happen, how it is judged.

A scenario is data. Its `steps` are what the customer writes, what the merchant's team does
(take over, release, block, unblock, approve or reject a draft, reply), when the clock moves and
when a simulated failure changes. Its `checks` decide compliance from the facts the run leaves
(`checks.py`). Its `world` says how the isolated world is built: the reference instant, the
simulated failures and, for the indirect-injection case, a poisoned copy of the corpus written
into the run's temporary directory (the real `docs/` is never touched).

`model_script` is the scripted model of the scenario (`script.py`): the INTENDED behaviour for
the success, ambiguous and failure cases, and a HOSTILE or OBEDIENT one for the attacks and the
state crossings, because the point there is that the code's locks hold whatever the model does.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from evaluation.conversations.checks import Check, CheckKind
from evaluation.conversations.script import ScriptFactory
from merchant_assistant.agent.domain.drafts import DraftKind
from merchant_assistant.control import Actor
from merchant_assistant.shared.faults import FaultMode

# Friday 2026-10-23, 12:00 in Buenos Aires: inside the default human attention window.
REFERENCE_INSTANT = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
_ID = re.compile(r"^[A-Z][0-9]{2}$|^X[0-9]$")
_TAG = re.compile(r"^[a-z][a-z0-9_]*$")


# `dev` scenarios are the ones the prompt and the evaluator were tuned against; `heldout` ones are
# new wordings of the same situations that nobody iterates on, so their numbers stay honest.
Split = Literal["dev", "heldout"]
SplitChoice = Literal["dev", "heldout", "all"]
SPLITS: tuple[SplitChoice, ...] = ("dev", "heldout", "all")


class Category(StrEnum):
    SUCCESS = "success"
    AMBIGUOUS = "ambiguous"
    TOOL_FAILURE = "tool_failure"
    POLICY_ATTACK = "policy_attack"
    STATE_CROSSING = "state_crossing"


class When(StrEnum):
    """When a customer message is sent. A live model words things its own way, so a reply
    that only makes sense after a question waits for that question to be visible."""

    ALWAYS = "always"
    CONFIRMATION_REQUESTED = "confirmation_requested"  # the last message shown asks to confirm
    EXECUTION_FAILED = "execution_failed"  # the last message shown says the effect failed


class TeamCommand(StrEnum):
    TAKEOVER = "takeover"
    RELEASE = "release"
    BLOCK = "block"
    UNBLOCK = "unblock"
    APPROVE = "approve"
    REJECT = "reject"
    REPLY = "reply"


ToolName = Literal["consultar_ventas", "crear_reclamo"]


@dataclass(frozen=True)
class Say:
    text: str
    tag: str | None = None
    when: When = When.ALWAYS


@dataclass(frozen=True)
class Team:
    command: TeamCommand
    actor: Actor = Actor.OPERATOR
    draft_kind: DraftKind | None = None  # approve/reject: the oldest pending draft of this kind
    text: str | None = None  # reply only
    tag: str | None = None

    def __post_init__(self) -> None:
        wants_kind = self.command in (TeamCommand.APPROVE, TeamCommand.REJECT)
        if wants_kind != (self.draft_kind is not None):
            raise ValueError(f"{self.command} {'needs' if wants_kind else 'takes no'} draft kind")
        if (self.command is TeamCommand.REPLY) != (self.text is not None):
            raise ValueError("only a reply carries a text, and it must")


@dataclass(frozen=True)
class MoveClock:
    to: datetime
    tag: str | None = None

    def __post_init__(self) -> None:
        if self.to.utcoffset() is None:
            raise ValueError("the clock moves to a timezone-aware instant")


@dataclass(frozen=True)
class FaultPlan:
    """Fail the first `first_calls` calls with `mode`, then succeed; every call when None."""

    mode: FaultMode
    first_calls: int | None = None

    def __post_init__(self) -> None:
        if self.first_calls is not None and self.first_calls < 1:
            raise ValueError("first_calls must be >= 1, or None for every call")

    def describe(self) -> str:
        scope = (
            "todas las llamadas" if self.first_calls is None else f"{self.first_calls} llamada(s)"
        )
        return f"{self.mode} en {scope}"


@dataclass(frozen=True)
class SetFaults:
    """Replace the simulated failures of one tool from now on (None switches them off)."""

    tool: ToolName
    plan: FaultPlan | None
    tag: str | None = None


Step: TypeAlias = Say | Team | MoveClock | SetFaults


@dataclass(frozen=True)
class CorpusPatch:
    """A poisoned copy of the corpus: `addition` is appended to the copy of `document`."""

    document: str  # for example "DOC-04"
    addition: str


@dataclass(frozen=True)
class WorldSpec:
    start: datetime = REFERENCE_INSTANT
    sales_faults: FaultPlan | None = None
    complaint_faults: FaultPlan | None = None
    corpus_patch: CorpusPatch | None = None

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None:
            raise ValueError("the world starts at a timezone-aware instant")


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    category: Category
    steps: tuple[Step, ...]
    expected_behaviour: str
    tools_expected: tuple[str, ...]
    tools_forbidden: tuple[str, ...]
    checks: tuple[Check, ...]
    model_script: ScriptFactory
    script_note: str  # what the scripted model does, in Spanish, for the report
    world: WorldSpec = field(default_factory=WorldSpec)
    # A real limitation of the agent this scenario documents: the checks named here are
    # EXPECTED to fail with the scripted model, and the validation pins exactly those.
    known_limitation: str | None = None
    expected_failures: tuple[str, ...] = ()
    split: Split = "dev"

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise ValueError(f"scenario id {self.id!r} must look like S01 or X1")
        if not any(isinstance(step, Say) for step in self.steps):
            raise ValueError(f"{self.id}: a scenario needs at least one customer message")
        tags = [step.tag for step in self.steps if step.tag is not None]
        if len(set(tags)) != len(tags) or not all(_TAG.match(tag) for tag in tags):
            raise ValueError(f"{self.id}: step tags must be unique snake_case names")
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.id}: two checks share a name")
        scored = [c for c in self.checks if c.kind is not CheckKind.EVIDENCE]
        if not any(check.probe.basis == "fact" for check in scored):
            raise ValueError(f"{self.id}: at least one scored check must rest on a fact")
        if (self.split == "heldout") != self.id.startswith("H"):
            raise ValueError(f"{self.id}: held-out scenarios, and only they, are numbered H01...")
        if bool(self.known_limitation) != bool(self.expected_failures):
            raise ValueError(f"{self.id}: a known limitation names the checks it fails")
        if not set(self.expected_failures) <= set(names):
            raise ValueError(f"{self.id}: an expected failure is not one of the checks")

    @property
    def user_messages(self) -> tuple[Say, ...]:
        return tuple(step for step in self.steps if isinstance(step, Say))

    def checks_of(self, kind: CheckKind) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.kind is kind)


def in_split(scenarios: Sequence[Scenario], split: SplitChoice) -> tuple[Scenario, ...]:
    """The scenarios of one split, in catalog order (`all` keeps every one)."""
    return tuple(s for s in scenarios if split == "all" or s.split == split)
