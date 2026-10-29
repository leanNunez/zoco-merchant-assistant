"""The harness of one scenario: its own database, clock, claims files and fault plan.

Everything a scenario does goes through the same harness code a real caller would use; the
world only prepares the starting state (what Zoco Chat or an earlier session left behind) and
simulates a restart by building new objects over the same files.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from cortex.bootstrap import BUSINESS_TIMEZONE, HarnessData, build_runner, zoco_tool_binder
from cortex.chat.service import ChatEvents
from cortex.config import RunLimits
from cortex.identity import AuthContext
from cortex.kit_adapter.audit import Audit
from cortex.kit_adapter.observables import CallCounter
from cortex.kit_adapter.preconditions import Setup
from cortex.kit_adapter.scenario import UnsupportedScenario
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.model import FinalAnswer, ModelPort, ModelTurn, Prompt
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.adapters.faults import FaultKind, FaultPlan
from cortex.tools.confirmation import canonical_arguments, fingerprint
from cortex.tools.policy import Approved, PolicyContext, review
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock, business_today

# The second turn of each interleaved conversation: it makes each one read its history after
# the other company has written to its own.
FOLLOW_UP = "¿Con quién estoy hablando?"
INTERLEAVED_REPLY = "OK"

# When, inside a run, a takeover reported by the scenario reaches the harness.
AFTER_REPLY = "after_response_generation_before_delivery"
AFTER_TOOL_REQUEST = "after_tool_request_before_execution"


class World:
    def __init__(
        self,
        data: HarnessData,
        workdir: Path,
        now: datetime,
        faults: Mapping[str, tuple[FaultKind, ...]],
        limits: RunLimits,
    ) -> None:
        self.data = data
        self.clock = FixedClock(now)
        # One clock for the whole scenario: simulated timeouts and waits move it.
        self.ticker = ManualTicker()
        self.faults = FaultPlan(faults, ticker=self.ticker, timeout_cost_ms=limits.tool_timeout_ms)
        self._path = workdir / "cortex.db"
        self.claims = ClaimsDesk(workdir / "claims", self.clock)
        self.counter = CallCounter()
        self.database = self._open(recover=False)
        self.audit = Audit(self._path, self.claims.directory)

    @property
    def chat(self) -> ChatEvents:
        return ChatEvents(self.database, self.data.directory, self.clock, BUSINESS_TIMEZONE)

    def restart(self) -> None:
        """A new process: new objects over the same files, after the startup recovery."""
        self.database = self._open(recover=True)

    def _open(self, *, recover: bool) -> Database:
        database = Database(self._path, self.clock)
        database.initialize()
        if recover:
            database.recover()
        return database

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
            bind_tools=self.counter.wrap(
                self.faults.wrap(zoco_tool_binder(self.data, self.claims))
            ),
            clock=self.clock,
            ticker=self.ticker,
        )
        return runner.run(RunRequest(run_id, conversation_id, message, auth, limits))

    # ------------------------------------------------------------------ starting state

    def seed(self, auth: AuthContext, conversation_id: str, setup: Setup) -> None:
        """Leave the conversation as the preconditions describe it."""
        store = self.database.for_tenant(auth)
        conversation = store.open_conversation(conversation_id)
        mode = setup.mode or conversation.mode
        version = (
            setup.control_version
            if setup.control_version is not None
            else conversation.control_version
        )
        store.chat.set_control(conversation_id, mode, version)
        for tool, operation_id in setup.pending:
            # Known only by kind and id. While a person is in control it cannot run.
            status = PendingStatus.PENDING if mode is State.AUTO else PendingStatus.SUSPENDED
            store.ledger.record_pending(
                PendingAction(operation_id, conversation_id, tool, None, None, version, status)
            )
        self._seed_confirmed(auth, conversation_id, setup.confirmed_actions, version)

    def _seed_confirmed(
        self,
        auth: AuthContext,
        conversation_id: str,
        actions: tuple[tuple[str, Mapping[str, object]], ...],
        version: int,
    ) -> None:
        """Writes the user confirmed earlier go through the same checks as any request."""
        context = PolicyContext(auth, State.AUTO, business_today(self.clock, BUSINESS_TIMEZONE))
        store = self.database.for_tenant(auth)
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
                    control_version=version,
                    status=PendingStatus.CONFIRMED,
                )
            )

    # ------------------------------------------------------------------ interleaving

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
            [(e.tenant_id, e.conversation_id, e.message) for e in setup.interleaved],
            [(e.tenant_id, e.conversation_id, FOLLOW_UP) for e in setup.interleaved],
        ]
        for turn in rounds:
            for tenant_id, conversation_id, message in turn:
                key = (tenant_id, conversation_id)
                model = ScriptedModel([{"final": INTERLEAVED_REPLY}])
                run_id = f"run-{conversation_id}-{len(seen.get(key, [])) + 1}"
                self.run(
                    self.auth_for(tenant_id), run_id, conversation_id, message, setup.limits, model
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

    def auth_for(self, tenant_id: str) -> AuthContext:
        principal = self.data.test_principals.get(tenant_id)
        if principal is None:
            raise UnsupportedScenario(f"no test principal for company {tenant_id!r}")
        return self.data.directory.authenticate(tenant_id, principal)


class TakeoverDuringRun:
    """A model whose generation an operator interrupts: the takeover lands right after the
    model produced the matching turn, before the harness acts on it. It is how the scenario
    simulates a concurrent operator without adding any hook to the harness itself."""

    def __init__(self, model: ModelPort, when: str, take_over: Callable[[], None]) -> None:
        if when not in (AFTER_REPLY, AFTER_TOOL_REQUEST):
            raise UnsupportedScenario(f"unknown moment for the takeover: {when!r}")
        self._model, self._when, self._take_over = model, when, take_over
        self._done = False

    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn:
        turn = self._model.next_turn(prompt, timeout_ms=timeout_ms)
        is_reply = isinstance(turn, FinalAnswer)
        if not self._done and is_reply == (self._when == AFTER_REPLY):
            self._done = True
            self._take_over()
        return turn
