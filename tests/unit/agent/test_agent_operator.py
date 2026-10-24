"""The team's commands: one store with the customer's turn, optimistic concurrency, effects."""

import random
from dataclasses import replace
from datetime import date, datetime, time
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.scripted_model import tool_call
from merchant_assistant.agent.application.conversation import Outbound
from merchant_assistant.agent.application.executor import ToolExecutor
from merchant_assistant.agent.application.operator import (
    OPERATOR_MESSAGE_LIMIT,
    CommandResult,
    DraftView,
    OperatorService,
)
from merchant_assistant.agent.domain.drafts import Draft, DraftKind, DraftStatus
from merchant_assistant.agent.domain.effects import apply_effects
from merchant_assistant.agent.domain.errors import (
    ConcurrentUpdate,
    DraftNotApprovable,
    InvalidOperatorMessage,
)
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.messages import Author, Message, Role
from merchant_assistant.agent.domain.pending import Confirmed, PendingAction, PendingState, propose
from merchant_assistant.agent.domain.pending import decide as gate
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.templates import execution_failed, registered
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, TOOL_SPECS
from merchant_assistant.agent.ports import Registration, SalesOutcome, SearchOutcome
from merchant_assistant.control import Actor, BusinessHours, Event, InvalidTransition, State
from merchant_assistant.shared.clock import FixedClock
from merchant_assistant.shared.errors import DomainError

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
HOURS = BusinessHours(
    tz=BUENOS_AIRES, opens=time(9, 0), closes=time(18, 0), weekdays=frozenset(range(5))
)
OPEN = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)
CID = "conv-1"


class RacingStore(InMemoryConversationStore):
    """Someone else (a customer turn) writes right before each of our first `races` saves."""

    def __init__(self, races: int) -> None:
        super().__init__()
        self.races = races

    def save(self, state: ConversationState, *, expected_version: int) -> None:
        if self.races > 0:
            self.races -= 1
            current = self.load(state.conversation_id) or ConversationState(state.conversation_id)
            intruder = replace(
                current.with_messages(Message(Role.USER, f"mensaje {self.races}")),
                version=current.version + 1,
            )
            super().save(intruder, expected_version=current.version)
        super().save(state, expected_version=expected_version)


class NoSearch:
    def search(self, query: str) -> SearchOutcome:
        return SearchOutcome("no_data", ())


class NoSales:
    def lookup(self, raw_date: str) -> SalesOutcome:
        return SalesOutcome("no_data", date.fromisoformat(raw_date), None)


class CountingRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        self.calls += 1
        return Registration("REC-000001", created=self.calls == 1)


def texts(result: CommandResult) -> list[str]:
    return [outbound.text for outbound in result.outbound]


def team(
    store: InMemoryConversationStore, registry: CountingRegistry | None = None
) -> OperatorService:
    executor = ToolExecutor(
        NoSearch(),
        NoSales(),
        registry or CountingRegistry(),
        sleeper=lambda _: None,
        rng=random.Random(0),  # noqa: S311
    )
    return OperatorService(store, executor, hours=HOURS, clock=FixedClock(OPEN))


def test_a_command_that_loses_a_race_is_replayed_on_the_fresh_state() -> None:
    store = RacingStore(races=2)

    result = team(store).takeover(CID, Actor.OPERATOR)

    stored = store.load(CID)
    assert stored is not None
    assert result.control_state is State.HUMAN
    # Both intruding messages survive: the command never overwrote what it did not read.
    assert [m.content for m in stored.messages] == ["mensaje 1", "mensaje 0"]
    assert len(stored.control_log) == 1


def test_a_command_that_keeps_losing_gives_up_loudly() -> None:
    with pytest.raises(ConcurrentUpdate):
        team(RacingStore(races=10)).takeover(CID, Actor.OPERATOR)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\n", "x" * (OPERATOR_MESSAGE_LIMIT + 1)],
    ids=["empty", "blank", "newline", "long"],
)
def test_an_operator_message_must_have_text_within_the_limit(text: str) -> None:
    store = InMemoryConversationStore()
    service = team(store)
    service.takeover(CID, Actor.OPERATOR)

    with pytest.raises(InvalidOperatorMessage) as refused:
        service.operator_reply(CID, Actor.OPERATOR, text)

    assert "x" * 20 not in str(refused.value.public())


def test_an_operator_message_is_trimmed_and_stored_as_the_operators() -> None:
    store = InMemoryConversationStore()
    service = team(store)
    service.takeover(CID, Actor.SUPERVISOR)

    result = service.operator_reply(CID, Actor.SUPERVISOR, "  Ya lo veo.  ")

    assert result.outbound == (Outbound("operator", "Ya lo veo."),)
    stored = store.load(CID)
    assert stored is not None
    assert stored.messages == (Message(Role.ASSISTANT, "Ya lo veo.", author=Author.OPERATOR),)


def test_the_draft_list_shows_only_what_still_waits() -> None:
    store = InMemoryConversationStore()
    waiting = Draft("D-0002", DraftKind.REPLY, "Espera.", OPEN, 2)
    decided = Draft(
        "D-0001",
        DraftKind.REPLY,
        "Ya enviado.",
        OPEN,
        1,
        status=DraftStatus.APPROVED,
        decided_by=Actor.OPERATOR,
        decided_at=OPEN,
    )
    store.save(ConversationState(CID, version=1, drafts=(decided, waiting)), expected_version=0)

    assert team(store).drafts(CID) == (DraftView("D-0002", DraftKind.REPLY, "Espera.", OPEN, 2),)
    assert team(store).drafts("nobody") == ()


def test_the_team_sees_an_approval_without_the_confirmation_it_holds() -> None:
    store = InMemoryConversationStore()
    store.save(awaiting_approval(), expected_version=0)

    (view,) = team(store).drafts(CID)

    assert view.kind is DraftKind.ACTION_APPROVAL
    assert not any(isinstance(value, Confirmed) for value in vars_of(view))


def vars_of(view: DraftView) -> list[object]:
    return [getattr(view, name) for name in view.__slots__]


# --------------------------------------------------------------------------- approving a complaint

COMPLAINT = {"motivo": "Pago doble", "descripcion": "Me cobraron dos veces la entrada."}


def awaiting_approval() -> ConversationState:
    """SUGGEST, the question delivered in turn 1, the customer's yes in turn 2: one draft."""
    control = ConversationState(CID).control()
    control.apply(Event.LOW_CONFIDENCE, Actor.SYSTEM, at=OPEN, hours=HOURS)
    validated = authorize(tool_call(CREAR_RECLAMO, COMPLAINT), TOOL_SPECS)
    action = PendingAction.create(validated, OPEN, turn=1, control_version=len(control.log))
    pending, _ = propose(PendingState(), action)
    _, confirmed = gate(pending, "sí", turn=2, control_version=len(control.log))
    assert isinstance(confirmed, Confirmed)
    draft = Draft("D-0001", DraftKind.ACTION_APPROVAL, "Aprobar.", OPEN, 2, confirmed=confirmed)
    return ConversationState(
        CID,
        version=1,
        turn=2,
        pending=pending,
        control_log=control.log.entries,
        drafts=(draft,),
    )


class FailingRegistry(CountingRegistry):
    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        self.calls += 1
        raise TimeoutFault("simulated timeout at /var/db with secret detail")


class TimeoutFault(DomainError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "tool_timeout"
    user_message: ClassVar[str] = "The service took too long."
    retryable: ClassVar[bool] = True


def test_a_failed_execution_tells_the_customer_honestly_and_keeps_the_action() -> None:
    store = InMemoryConversationStore()
    store.save(awaiting_approval(), expected_version=0)
    registry = FailingRegistry()

    result = team(store, registry).approve_draft(CID, "D-0001", Actor.OPERATOR)

    assert registry.calls == 3  # retried, with the same parameters
    assert texts(result) == [execution_failed()]
    assert "crear_reclamo:failed:tool_timeout" in result.events
    assert result.registered == ()
    stored = store.load(CID)
    assert stored is not None
    assert stored.pending.pending is not None  # a later yes asks the operator again
    assert all("secret" not in (m.content or "") for m in stored.messages)


def test_a_lost_race_after_the_complaint_ran_never_runs_it_twice() -> None:
    store = RacingStore(races=0)
    store.save(awaiting_approval(), expected_version=0)
    store.races = 1  # a customer message lands between our load and our save
    registry = CountingRegistry()

    result = team(store, registry).approve_draft(CID, "D-0001", Actor.OPERATOR)

    assert registry.calls == 1
    assert texts(result) == [registered("REC-000001", created=True)]
    assert result.registered == ("REC-000001",)


class TakeoverRace(InMemoryConversationStore):
    """Right before our first save, an operator takes the conversation over."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False

    def save(self, state: ConversationState, *, expected_version: int) -> None:
        if self.armed:
            self.armed = False
            current = self.load(state.conversation_id)
            assert current is not None
            control = current.control()
            outcome = control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=OPEN, hours=HOURS)
            moved, _ = apply_effects(current.with_control(control), outcome, at=OPEN)
            super().save(
                replace(moved, version=current.version + 1), expected_version=current.version
            )
        super().save(state, expected_version=expected_version)


def test_a_complaint_stored_while_control_changed_hands_is_recorded_but_not_announced() -> None:
    store = TakeoverRace()
    store.save(awaiting_approval(), expected_version=0)
    store.armed = True
    registry = CountingRegistry()

    result = team(store, registry).approve_draft(CID, "D-0001", Actor.OPERATOR)

    assert registry.calls == 1
    assert result.outbound == ()
    assert result.control_state is State.HUMAN
    stored = store.load(CID)
    assert stored is not None
    assert stored.last_registered_id == "REC-000001"
    assert stored.pending.pending is None
    assert [(n.code, n.detail) for n in stored.inbox] == [
        ("complaint_registered_unannounced", "REC-000001")
    ]
    assert all(m.role is not Role.ASSISTANT for m in stored.messages)


def test_an_approval_under_another_control_version_is_refused_and_discarded() -> None:
    state = awaiting_approval()
    control = state.control()
    with pytest.raises(InvalidTransition):  # a refused attempt still bumps the version
        control.apply(Event.OPERATOR_BLOCK, Actor.OPERATOR, at=OPEN, hours=HOURS)
    store = InMemoryConversationStore()
    store.save(state.with_control(control), expected_version=0)
    registry = CountingRegistry()

    with pytest.raises(DraftNotApprovable):
        team(store, registry).approve_draft(CID, "D-0001", Actor.OPERATOR)

    assert registry.calls == 0
    stored = store.load(CID)
    assert stored is not None
    assert [d.status for d in stored.drafts] == [DraftStatus.DISCARDED]
