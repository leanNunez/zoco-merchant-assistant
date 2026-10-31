"""The harness of one scenario: its own database, clock, claims files and fault plan.

Everything a scenario does goes through the same harness code a real caller would use; the
world only prepares the starting state (what Zoco Chat or an earlier session left behind) and
simulates a restart by building new objects over the same files.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from cortex.bootstrap import (
    BUSINESS_TIMEZONE,
    DEFAULT_TRACE_KEY,
    HarnessData,
    build_runner,
    zoco_tool_binder,
)
from cortex.chat.service import ChatEvents
from cortex.config import RunLimits
from cortex.identifiers import OPERATION_ID_PATTERN
from cortex.identity import AuthContext
from cortex.kit_adapter.audit import Audit
from cortex.kit_adapter.observables import CallCounter
from cortex.kit_adapter.preconditions import Setup
from cortex.kit_adapter.scenario import UnsupportedScenario, run_id_for
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.memory.tenant_store import TenantStore
from cortex.observability.redaction import TraceRedactor
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.finish import FinishDetail
from cortex.runner.model import FinalAnswer, ModelPort, ModelTurn, Prompt
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from cortex.tools.adapters.faults import FaultKind, FaultPlan
from cortex.tools.confirmation import canonical_arguments, fingerprint
from cortex.tools.policy import Approved, PolicyContext, RejectionCode, review
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock, business_today

# The second turn of each interleaved conversation: it makes each one read its history after
# the other company has written to its own.
FOLLOW_UP = "¿Con quién estoy hablando?"
INTERLEAVED_REPLY = "OK"

# The world's clock is simulated: a simulated timeout costs its limit on `ticker` without waiting.
# A real wait, then, must never decide an outcome, so none is shorter than this (an adapter of
# the world answers in a few milliseconds).
REAL_WAIT_FLOOR_MS = 2000

# The endings of a run that refused its message before storing it.
REFUSED_BEFORE_STORING = frozenset(
    {
        FinishDetail.MESSAGE_TOO_LONG,
        FinishDetail.STALE_VERSION,
        FinishDetail.CONVERSATION_BUSY,
        FinishDetail.RUN_ID_CONFLICT,
        FinishDetail.RUN_INTERRUPTED,
    }
)

# When, inside a run, a takeover reported by the scenario reaches the harness.
AFTER_REPLY = "after_response_generation_before_delivery"
AFTER_TOOL_REQUEST = "after_tool_request_before_execution"
BEFORE_RUN = "before_run"


class World:
    def __init__(
        self,
        data: HarnessData,
        workdir: Path,
        now: datetime,
        faults: Mapping[str, tuple[FaultKind, ...]],
        limits: RunLimits,
        trace_key: str = DEFAULT_TRACE_KEY,
    ) -> None:
        self.data = data
        self.trace_key = trace_key
        self.clock = FixedClock(now)
        # One clock for the whole scenario: simulated timeouts and waits move it.
        self.ticker = ManualTicker()
        # A simulated timeout costs what a real attempt could wait: its own limit, never more
        # than the whole run (the budget clamps every attempt the same way).
        timeout_cost_ms = min(limits.tool_timeout_ms, limits.total_timeout_ms)
        self.faults = FaultPlan(faults, ticker=self.ticker, timeout_cost_ms=timeout_cost_ms)
        self._path = workdir / "cortex.db"
        self.claims = ClaimsDesk(workdir / "claims", self.clock)
        self.counter = CallCounter()
        self.database = self._open(recover=False)
        self.audit = Audit(self._path, self.claims)
        self._load_contacts()

    def _load_contacts(self) -> None:
        """Each company's contacts from the kit, written through that company's own store."""
        for tenant_id, contacts in self.data.contacts.items():
            store = self.database.for_tenant(self.auth_for(tenant_id))
            for contact in contacts:
                store.record_contact(contact.contact_id, contact.name)

    @property
    def chat(self) -> ChatEvents:
        return ChatEvents(
            self.database,
            self.data.directory,
            self.clock,
            BUSINESS_TIMEZONE,
            TraceRedactor(self.trace_key),
        )

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
        *,
        tools: frozenset[str] | None = None,
        expected_control_version: int | None = None,
        before_delivery: Callable[[], None] | None = None,
    ) -> RunResult:
        """`before_delivery`, when given, is called right before each delivery the run tries."""
        database = self.database
        if before_delivery is not None:
            database = _WatchedDeliveries(database, before_delivery)
        runner = build_runner(
            data=self.data,
            database=database,
            model=model,
            bind_tools=self.counter.wrap(
                self.faults.wrap(zoco_tool_binder(self.data, self.claims, clock=self.clock))
            ),
            clock=self.clock,
            ticker=self.ticker,
            trace_key=self.trace_key,
            real_wait_floor_ms=REAL_WAIT_FLOOR_MS,
        )
        # Without a tool list, the whole catalog; without an expected control version, the one
        # found in the database.
        request = RunRequest(
            run_id,
            conversation_id,
            message,
            auth,
            limits,
            tools=tools,
            expected_control_version=expected_control_version,
        )
        return runner.run(request)

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
        """Writes the user confirmed earlier, as the scenario gives them.

        Values that pass the checks are stored normalized, exactly like a confirmation the
        harness asked for. Values the checks refuse (an invalid field, a scope field) are stored
        as given: the confirmation is a fact of the past, and the runtime then refuses the
        request that uses them like any other. A tool outside the catalog, or values without an
        operation_id to key them on, cannot be stored at all.
        """
        context = PolicyContext(auth, State.AUTO, business_today(self.clock, BUSINESS_TIMEZONE))
        store = self.database.for_tenant(auth)
        for tool, arguments in actions:
            verdict = review(
                ToolCall("seed", tool, json.dumps(arguments)), self.data.catalog, context
            )
            if isinstance(verdict, Approved):
                operation_id = str(getattr(verdict.arguments, "operation_id", ""))
                stored, values = (
                    canonical_arguments(verdict.arguments),
                    fingerprint(tool, verdict.arguments),
                )
            elif verdict.code in _SEEDED_AS_GIVEN:
                operation_id = _seed_operation_id(tool, arguments)
                stored, values = _as_given(tool, arguments)
            else:
                raise UnsupportedScenario(f"a confirmed action does not pass the checks: {tool}")
            store.ledger.record_pending(
                PendingAction(
                    operation_id=operation_id,
                    conversation_id=conversation_id,
                    tool=tool,
                    arguments=stored,
                    fingerprint=values,
                    control_version=version,
                    status=PendingStatus.CONFIRMED,
                )
            )

    # ------------------------------------------------------------------ interleaving

    def interleave(self, setup: Setup, writers: Sequence[AuthContext]) -> bool:
        """Run the interleaved conversations turn by turn and check they never mixed.

        Round one sends each listed message in order; round two sends a follow-up to each
        conversation, so every conversation reads its history after the others wrote theirs.
        Separated means: each model saw only its own conversation's messages, the second turn
        still saw the first one, each conversation stored only what was sent to it (a message
        the runner refused before storing it is not expected anywhere), and each
        company sees exactly its own contacts (in the kit, its own "Alex" and never the other's).
        `writers[i]` is who writes the i-th interleaved conversation, already authenticated.
        """
        seen: dict[tuple[str, str], list[ScriptedModel]] = {}
        sent: dict[tuple[str, str], set[str]] = {}
        entries = list(zip(setup.interleaved, writers, strict=True))
        rounds = [
            [(auth, e.conversation_id, e.message) for e, auth in entries],
            [(auth, e.conversation_id, FOLLOW_UP) for e, auth in entries],
        ]
        pending_run_ids = iter(interleaved_run_ids(setup))
        for turn in rounds:
            for auth, conversation_id, message in turn:
                key = (auth.tenant_id, conversation_id)
                model = ScriptedModel([{"final": INTERLEAVED_REPLY}])
                run_id = next(pending_run_ids)
                result = self.run(auth, run_id, conversation_id, message, setup.limits, model)
                seen.setdefault(key, []).append(model)
                to_store = sent.setdefault(key, {INTERLEAVED_REPLY})
                # A message refused before it was stored (too long, say) crossed nowhere.
                if result.finish_detail not in REFUSED_BEFORE_STORING:
                    to_store.add(message)
        conversations = all(self._only_own(key, models, sent[key]) for key, models in seen.items())
        return conversations and all(self._own_contacts(auth) for auth in writers)

    def _own_contacts(self, auth: AuthContext) -> bool:
        """Every name the company's contacts go by finds exactly that company's contacts."""
        own = self.data.contacts.get(auth.tenant_id, ())
        store = self.database.for_tenant(auth)
        return set(store.contacts()) == set(own) and all(
            {c.contact_id for c in store.contacts(name=contact.name)}
            == {c.contact_id for c in own if c.name == contact.name}
            for contact in own
        )

    def _only_own(
        self, key: tuple[str, str], models: list[ScriptedModel], expected: set[str]
    ) -> bool:
        stored = self.audit.messages(*key)
        prompts = [{m.content for p in model.prompts for m in p.messages} for model in models]
        first_message = next(iter(expected - {INTERLEAVED_REPLY, FOLLOW_UP}), None)
        remembers = first_message is None or first_message in prompts[-1]
        return stored == expected and all(p <= stored for p in prompts) and remembers

    def auth_for(self, tenant_id: str, principal_id: str | None = None) -> AuthContext:
        """The context of `principal_id` in that company, or of the kit's test principal there.
        Raises AuthenticationFailed when the principal does not belong to the company."""
        principal = principal_id or self.data.test_principals.get(tenant_id)
        if principal is None:
            raise UnsupportedScenario(f"no test principal for company {tenant_id!r}")
        return self.data.directory.authenticate(tenant_id, principal)


def interleaved_run_ids(setup: Setup) -> list[str]:
    """The run id of every interleaved turn, in the order they run: round one, then round two.

    Every one is derived and checked before anything runs: an id that is too long is an
    unsupported scenario, not a crash in the middle of the second round.
    """
    run_ids: list[str] = []
    counts: dict[tuple[str, str], int] = {}
    for entry in (*setup.interleaved, *setup.interleaved):
        key = (entry.tenant_id, entry.conversation_id)
        counts[key] = counts.get(key, 0) + 1
        run_ids.append(run_id_for(entry.conversation_id, counts[key]))
    return run_ids


# The refusals of a confirmed action's values that the runtime itself decides when they are used.
_SEEDED_AS_GIVEN = frozenset({RejectionCode.INVALID_ARGUMENTS, RejectionCode.SCOPE_VIOLATION})


def _seed_operation_id(tool: str, arguments: Mapping[str, object]) -> str:
    operation_id = arguments.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or re.fullmatch(OPERATION_ID_PATTERN, operation_id) is None
    ):
        raise UnsupportedScenario(f"a confirmed action of {tool} needs a valid operation_id")
    return operation_id


def _as_given(tool: str, arguments: Mapping[str, object]) -> tuple[str, str]:
    """(canonical JSON, fingerprint) of values no schema validated, in the same shape as
    `canonical_arguments` and `fingerprint`."""
    stored = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    keyed = json.dumps(
        {"tool": tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return stored, hashlib.sha256(keyed.encode("utf-8")).hexdigest()


class TakeoverDuringRun:
    """A model whose tool request an operator interrupts: the takeover lands right after the
    model asked for its first tool, before the harness acts on it. It is how the scenario
    simulates a concurrent operator without adding any hook to the harness itself. A run that
    never asks for a tool gets it right after it ends (`land`). Either way it lands exactly
    once."""

    def __init__(self, model: ModelPort, take_over: Callable[[], None]) -> None:
        self._model, self._take_over = model, take_over
        self._done = False

    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn:
        turn = self._model.next_turn(prompt, timeout_ms=timeout_ms)
        if not isinstance(turn, FinalAnswer):
            self.land()
        return turn

    def land(self) -> None:
        if not self._done:
            self._done = True
            self._take_over()


class TakeoverBeforeDelivery:
    """A takeover that lands right before the run's first delivery attempt, whoever wrote the
    text: the model's answer, the confirmation question of a write, the refusal of a scope
    violation. A run that never tries to deliver gets it right after it ends (`land`). Either
    way it lands exactly once, and the delivery's own compare-and-set suppresses the reply."""

    def __init__(self, take_over: Callable[[], None]) -> None:
        self._take_over = take_over
        self._landed = False

    def land(self) -> None:
        if not self._landed:
            self._landed = True
            self._take_over()


class _WatchedDeliveries(Database):
    """The world's database as the run sees it: every delivery first calls `before_delivery`."""

    def __init__(self, database: Database, before_delivery: Callable[[], None]) -> None:
        super().__init__(database.path, database.clock)
        self._before_delivery = before_delivery

    def for_tenant(self, auth: AuthContext) -> TenantStore:
        return _WatchedStore(self, auth, self._before_delivery)


class _WatchedStore(TenantStore):
    def __init__(
        self, database: Database, auth: AuthContext, before_delivery: Callable[[], None]
    ) -> None:
        super().__init__(database, auth)
        self._before_delivery = before_delivery

    def deliver_if_current(
        self,
        conversation_id: str,
        run_id: str,
        text: str,
        expected_version: int,
        *,
        reply_to: int | None,
    ) -> bool:
        self._before_delivery()
        return super().deliver_if_current(
            conversation_id, run_id, text, expected_version, reply_to=reply_to
        )
