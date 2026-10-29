"""Runs one kit scenario end to end and returns the contract's output.

Each scenario gets a fresh, isolated world: its own SQLite file in a temporary directory, a
fixed clock and a manual ticker. Nothing leaks from one scenario to the next, and running the
same scenario twice gives the same output.

Preconditions are read first, by one handler per key; an unknown key stops the scenario
(exit code 2) instead of being ignored.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from cortex.bootstrap import (
    BUSINESS_TIMEZONE,
    HarnessData,
    build_runner,
    load_harness_data,
    zoco_tool_binder,
)
from cortex.config import ConfigError, RunLimits
from cortex.identity import AuthContext, AuthenticationFailed
from cortex.kit_adapter.audit import Audit
from cortex.kit_adapter.observables import CallCounter, build_output
from cortex.kit_adapter.scenario import Scenario, UnsupportedScenario, load_scenario
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.budget import ManualTicker
from cortex.runner.model import ModelPort
from cortex.runner.scripted_model import ScriptedModel
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.confirmation import canonical_arguments, fingerprint
from cortex.tools.policy import Approved, PolicyContext, review
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock, business_today

# The instant of the contract's example. Fixed, so a decision that depends on "today" does not
# change with the day the evaluation is run; `--now` overrides it.
DEFAULT_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# The second turn of each interleaved conversation: it makes each one read its history after
# the other company has written to its own.
FOLLOW_UP = "¿Con quién estoy hablando?"
INTERLEAVED_REPLY = "OK"


@dataclass(frozen=True, slots=True)
class InterleavedMessage:
    tenant_id: str
    conversation_id: str
    message: str


@dataclass(frozen=True, slots=True)
class Setup:
    """What the preconditions ask for, before anything runs."""

    limits: RunLimits
    interleaved: tuple[InterleavedMessage, ...] = field(default_factory=tuple)
    # Writes the user already confirmed before the scenario starts: (tool, arguments).
    confirmed_actions: tuple[tuple[str, Mapping[str, object]], ...] = field(default_factory=tuple)


def run_scenario_file(
    path: Path, *, kit_dir: Path, now: datetime = DEFAULT_NOW
) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise UnsupportedScenario(f"cannot read {path.name}: {unreadable}") from None
    if not isinstance(raw, dict):
        raise UnsupportedScenario("a scenario must be a JSON object")
    scenario = load_scenario(raw)
    with tempfile.TemporaryDirectory(prefix="cortex-") as workdir:
        return run_scenario(scenario, kit_dir=kit_dir, workdir=Path(workdir), now=now)


def run_scenario(
    scenario: Scenario, *, kit_dir: Path, workdir: Path, now: datetime
) -> dict[str, object]:
    data = load_harness_data(kit_dir)
    setup = read_preconditions(scenario.preconditions, data.config.limits)
    if scenario.faults or scenario.incoming_event is not None:
        raise UnsupportedScenario("faults and incoming events are not supported yet")

    world = _World(data, workdir, now)
    run_id = f"run-{scenario.conversation_id}"
    try:
        auth = data.directory.authenticate(scenario.tenant_id, scenario.principal_id)
    except AuthenticationFailed:
        return _unauthenticated(scenario, run_id, setup.limits)

    separated = world.interleave(setup) if setup.interleaved else None
    world.seed_confirmed(auth, scenario.conversation_id, setup.confirmed_actions)
    result = world.run(
        auth,
        run_id,
        scenario.conversation_id,
        scenario.message,
        setup.limits,
        ScriptedModel(scenario.model_script),
    )
    return build_output(
        result,
        store=world.database.for_tenant(auth),
        counter=world.counter,
        audit=world.audit,
        contexts_separated=separated,
    )


# --------------------------------------------------------------------------- preconditions


def read_preconditions(preconditions: Mapping[str, object], limits: RunLimits) -> Setup:
    setup = Setup(limits)
    for key, value in preconditions.items():
        handler = _PRECONDITIONS.get(key)
        if handler is None:
            raise UnsupportedScenario(f"precondition {key!r} is not supported")
        setup = handler(setup, value)
    return setup


def _max_steps(setup: Setup, value: object) -> Setup:
    if type(value) is not int:
        raise UnsupportedScenario("max_steps must be an integer")
    try:
        return replace(setup, limits=setup.limits.with_max_steps(value))
    except ConfigError as invalid:
        raise UnsupportedScenario(invalid.detail) from None


def _interleaved_conversations(setup: Setup, value: object) -> Setup:
    if not isinstance(value, list) or not value:
        raise UnsupportedScenario("interleaved_conversations must be a non-empty list")
    entries = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"tenant_id", "conversation_id", "message"}:
            raise UnsupportedScenario(
                "each interleaved conversation needs exactly tenant_id, conversation_id and message"
            )
        if not all(isinstance(text, str) and text for text in item.values()):
            raise UnsupportedScenario("interleaved conversation fields must be non-empty strings")
        entries.append(InterleavedMessage(**item))
    return replace(setup, interleaved=tuple(entries))


def _confirmed_actions(setup: Setup, value: object) -> Setup:
    if not isinstance(value, list):
        raise UnsupportedScenario("confirmed_actions must be a list")
    actions = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"tool", "args"}:
            raise UnsupportedScenario("each confirmed action needs exactly tool and args")
        if not isinstance(item["tool"], str) or not isinstance(item["args"], dict):
            raise UnsupportedScenario("a confirmed action is a tool name and an args object")
        actions.append((item["tool"], item["args"]))
    return replace(setup, confirmed_actions=tuple(actions))


_PRECONDITIONS: Mapping[str, Callable[[Setup, object], Setup]] = MappingProxyType(
    {
        "max_steps": _max_steps,
        "interleaved_conversations": _interleaved_conversations,
        "confirmed_actions": _confirmed_actions,
    }
)


# --------------------------------------------------------------------------- the world


class _World:
    """The harness of one scenario: one database, one clock, one call counter."""

    def __init__(self, data: HarnessData, workdir: Path, now: datetime) -> None:
        self.data = data
        self.clock = FixedClock(now)
        self.database = Database(workdir / "cortex.db", self.clock)
        self.database.initialize()
        self.claims = ClaimsDesk(workdir / "claims", self.clock)
        self.audit = Audit(self.database.path, self.claims.directory)
        self.counter = CallCounter()

    def run(
        self,
        auth: AuthContext,
        run_id: str,
        conversation_id: str,
        message: str,
        limits: RunLimits,
        model: ModelPort,
    ) -> RunResult:
        runner = build_runner(
            data=self.data,
            database=self.database,
            model=model,
            bind_tools=self.counter.wrap(zoco_tool_binder(self.data, self.claims)),
            clock=self.clock,
            ticker=ManualTicker(),
        )
        return runner.run(RunRequest(run_id, conversation_id, message, auth, limits))

    def seed_confirmed(
        self,
        auth: AuthContext,
        conversation_id: str,
        actions: tuple[tuple[str, Mapping[str, object]], ...],
    ) -> None:
        """Record writes the user confirmed earlier, through the same checks as any request."""
        if not actions:
            return
        store = self.database.for_tenant(auth)
        conversation = store.open_conversation(conversation_id)
        context = PolicyContext(auth, State.AUTO, business_today(self.clock, BUSINESS_TIMEZONE))
        for tool, arguments in actions:
            verdict = review(
                ToolCall("seed", tool, json.dumps(arguments)), self.data.catalog, context
            )
            if not isinstance(verdict, Approved):
                raise UnsupportedScenario(f"a confirmed action does not pass the checks: {tool}")
            store.ledger.record_pending(
                PendingAction(
                    operation_id=str(getattr(verdict.arguments, "operation_id", "")),
                    conversation_id=conversation_id,
                    tool=tool,
                    arguments=canonical_arguments(verdict.arguments),
                    fingerprint=fingerprint(tool, verdict.arguments),
                    control_version=conversation.control_version,
                    status=PendingStatus.CONFIRMED,
                )
            )

    def interleave(self, setup: Setup) -> bool:
        """Run the interleaved conversations turn by turn and check they never mixed.

        Round one sends each listed message in order; round two sends a follow-up to each
        conversation, so every conversation reads its history after the others wrote theirs.
        Separated means: each model saw only its own conversation's messages, the second turn
        still saw the first one, and each conversation stored only what was sent to it.
        """
        seen: dict[tuple[str, str], list[ScriptedModel]] = {}
        sent: dict[tuple[str, str], set[str]] = {}
        rounds = [
            [
                (entry.tenant_id, entry.conversation_id, entry.message)
                for entry in setup.interleaved
            ],
            [(entry.tenant_id, entry.conversation_id, FOLLOW_UP) for entry in setup.interleaved],
        ]
        for turn in rounds:
            for tenant_id, conversation_id, message in turn:
                key = (tenant_id, conversation_id)
                model = ScriptedModel([{"final": INTERLEAVED_REPLY}])
                run_id = f"run-{conversation_id}-{len(seen.get(key, [])) + 1}"
                self.run(
                    self._auth(tenant_id), run_id, conversation_id, message, setup.limits, model
                )
                seen.setdefault(key, []).append(model)
                sent.setdefault(key, {INTERLEAVED_REPLY}).add(message)
        return all(self._only_own(key, models, sent[key]) for key, models in seen.items())

    def _only_own(
        self, key: tuple[str, str], models: list[ScriptedModel], expected: set[str]
    ) -> bool:
        stored = self.audit.messages(*key)
        prompts = [{m.content for p in model.prompts for m in p.messages} for model in models]
        first_message = next(iter(expected - {INTERLEAVED_REPLY, FOLLOW_UP}), None)
        remembers = first_message is None or first_message in prompts[-1]
        return stored == expected and all(p <= stored for p in prompts) and remembers

    def _auth(self, tenant_id: str) -> AuthContext:
        principal = self.data.test_principals.get(tenant_id)
        if principal is None:
            raise UnsupportedScenario(f"no test principal for company {tenant_id!r}")
        return self.data.directory.authenticate(tenant_id, principal)


def _unauthenticated(scenario: Scenario, run_id: str, limits: RunLimits) -> dict[str, object]:
    """The caller is not who it claims: nothing runs and nothing is stored."""
    return {
        "run_id": run_id,
        "tenant_id": scenario.tenant_id,
        "conversation_id": scenario.conversation_id,
        "finish_reason": "BLOCKED",
        "finish_detail": "auth_invalid",
        "step_count": 0,
        "max_steps": limits.max_steps,
        "events": [{"seq": 1, "type": "run_rejected", "reason": "auth_invalid"}],
        "effects": {"claims_created": 0, "customer_messages_sent": 0},
        "state": {"conversation_mode": None, "control_version": None},
    }
