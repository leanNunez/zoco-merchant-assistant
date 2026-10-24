"""The team's commands: one store with the customer's turn, optimistic concurrency, effects."""

from dataclasses import replace
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.application.conversation import Outbound
from merchant_assistant.agent.application.operator import OPERATOR_MESSAGE_LIMIT, OperatorService
from merchant_assistant.agent.domain.drafts import Draft, DraftKind, DraftStatus
from merchant_assistant.agent.domain.errors import ConcurrentUpdate, InvalidOperatorMessage
from merchant_assistant.agent.domain.messages import Author, Message, Role
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.control import Actor, BusinessHours, State
from merchant_assistant.shared.clock import FixedClock

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


def team(store: InMemoryConversationStore) -> OperatorService:
    return OperatorService(store, hours=HOURS, clock=FixedClock(OPEN))


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

    assert team(store).drafts(CID) == (waiting,)
    assert team(store).drafts("nobody") == ()
