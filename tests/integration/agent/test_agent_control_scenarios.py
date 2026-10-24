"""Conversations that cross states of the control machine (brief, Parts 3 and 4).

The customer's turn (`ConversationService`) and the team's commands (`OperatorService`) share
one store, as in production. Complaints are stored by the real `ComplaintService` in a real
SQLite file, sales come from a real JSON file, and the model is a `ScriptedChatModel`. Every
test asserts observable facts: what reached the customer, rows in the database, the control
log in the stored state, and what the model was shown.
"""

import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.scripted_model import (
    ScriptedChatModel,
    ScriptStep,
    calls_reply,
    text_reply,
    tool_call,
)
from merchant_assistant.agent.application.conversation import (
    ConversationService,
    Outbound,
    TurnResult,
)
from merchant_assistant.agent.application.executor import ToolExecutor
from merchant_assistant.agent.application.operator import CommandResult, OperatorService
from merchant_assistant.agent.application.prompts import describe_hours
from merchant_assistant.agent.domain.errors import CommandNotAllowed, OperatorReplyNotAllowed
from merchant_assistant.agent.domain.messages import Author, Message, ModelReply, Role
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.templates import (
    blocked_notice,
    hours_notice,
    human_unavailable,
    nothing_pending,
)
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, ToolSpec
from merchant_assistant.agent.ports import Passage, SearchOutcome
from merchant_assistant.bootstrap import ComplaintsAdapter, SalesAdapter
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.control import (
    Actor,
    AttemptOutcome,
    BusinessHours,
    Event,
    GuardRejected,
    InvalidTransition,
    State,
    UnauthorizedActor,
)
from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.service import SalesService

pytestmark = pytest.mark.integration

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
HOURS = BusinessHours(
    tz=BUENOS_AIRES, opens=time(9, 0), closes=time(18, 0), weekdays=frozenset(range(5))
)
WINDOW = describe_hours(HOURS)
OPEN = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)  # a Friday, inside the window
CLOSED = datetime(2026, 10, 23, 21, 0, tzinfo=BUENOS_AIRES)  # the same Friday, after closing
CID = "conv-1"
A = {"motivo": "Entrada no recibida", "descripcion": "Pagué la entrada y no me llegó el correo."}
SALES = {
    "currency": "ARS",
    "days": [
        {
            "fecha": "2026-10-22",
            "entradas": 120,
            "bruto": "1500000.00",
            "devoluciones": "15000.00",
            "neto": "1485000.00",
        }
    ],
}


# --------------------------------------------------------------------------- hand-written fakes


@dataclass
class MovableClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


class FakeSearch:
    """Passages per exact query; any other query finds nothing."""

    def __init__(self, passages: Mapping[str, Sequence[Passage]] | None = None) -> None:
        self._passages = dict(passages or {})
        self.queries: list[str] = []

    def search(self, query: str) -> SearchOutcome:
        self.queries.append(query)
        found = tuple(self._passages.get(query, ()))
        return SearchOutcome("ok" if found else "no_data", found)


@dataclass
class World:
    agent: ConversationService
    team: OperatorService
    model: ScriptedChatModel
    store: InMemoryConversationStore
    repository: SqliteComplaintRepository
    clock: MovableClock
    search: FakeSearch

    def say(self, text: str) -> TurnResult:
        return self.agent.handle_user_message(CID, text)

    @property
    def rows(self) -> int:
        return self.repository.count()

    def stored(self) -> ConversationState:
        state = self.store.load(CID)
        assert state is not None
        return state


def make_world(
    directory: Path,
    steps: Iterable[ScriptStep] = (),
    *,
    now: datetime = OPEN,
    passages: Mapping[str, Sequence[Passage]] | None = None,
) -> World:
    clock = MovableClock(now)
    sales_path = directory / "ventas.json"
    sales_path.write_text(json.dumps(SALES), encoding="utf-8")
    sales = SalesAdapter(SalesService(JsonSalesRepository(sales_path), clock, BUENOS_AIRES))
    repository = SqliteComplaintRepository(directory / "reclamos.db")
    repository.initialize()
    complaints = ComplaintsAdapter(ComplaintService(repository, clock))
    search = FakeSearch(passages)
    executor = ToolExecutor(
        search,
        sales,
        complaints,
        sleeper=lambda _: None,
        rng=random.Random(7),  # noqa: S311
    )
    model = ScriptedChatModel(steps)
    store = InMemoryConversationStore()
    agent = ConversationService(store, executor, model, hours=HOURS, clock=clock)
    team = OperatorService(store, hours=HOURS, clock=clock)
    return World(agent, team, model, store, repository, clock, search)


def propose_reply(arguments: Mapping[str, object] = A) -> ModelReply:
    return calls_reply(tool_call(CREAR_RECLAMO, arguments))


def texts(result: TurnResult | CommandResult) -> list[str]:
    return [outbound.text for outbound in result.outbound]


def passage(doc_id: str, text: str) -> Passage:
    return Passage(doc_id, text, date(2026, 1, 1), date(2027, 12, 31))


def log_rows(world: World) -> list[tuple[State, State, Event, Actor, AttemptOutcome, str | None]]:
    return [
        (r.previous_state, r.new_state, r.event, r.actor, r.outcome, r.reason)
        for r in world.stored().control_log
    ]


# --------------------------------------------------------------------------- 1: takeover


def test_1_a_takeover_kills_a_half_confirmed_complaint_for_good(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    asked = world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    assert asked.pending is not None

    taken = world.team.takeover(CID, Actor.OPERATOR)
    silent = world.say("sí")
    world.team.release(CID, Actor.OPERATOR)
    late = world.say("sí")

    assert taken.control_state is State.HUMAN
    assert "pending_action_invalidated" in taken.events
    assert (silent.outbound, silent.model_calls, silent.registered) == ((), 0, ())
    assert texts(late) == [nothing_pending(None)]
    assert world.rows == 0
    assert world.model.call_count == 1


# --------------------------------------------------------------------------- 3: BLOCKED


def test_3_only_a_supervisor_brings_a_blocked_conversation_back(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Te pido respeto."), text_reply("¡Hola de nuevo!")])
    world.say("sos un idiota")
    blocked = world.say("idiota")

    ignored = [world.say(text) for text in ("¿hola?", "quiero hablar con una persona")]
    with pytest.raises(UnauthorizedActor):
        world.team.unblock(CID, Actor.OPERATOR)
    with pytest.raises(UnauthorizedActor):
        world.team.release(CID, Actor.OPERATOR)
    assert world.stored().control_state is State.BLOCKED
    unblocked = world.team.unblock(CID, Actor.SUPERVISOR)
    back = world.say("perdón, ¿cómo cobro?")

    assert blocked.outbound == (Outbound("notice", blocked_notice()),)
    assert all((r.outbound, r.model_calls) == ((), 0) for r in ignored)
    stored_users = [m.content for m in world.stored().messages if m.role is Role.USER]
    assert stored_users[2:4] == ["¿hola?", "quiero hablar con una persona"]
    assert unblocked.control_state is State.AUTO
    assert texts(back) == ["¡Hola de nuevo!"]
    assert log_rows(world) == [
        (
            State.AUTO,
            State.BLOCKED,
            Event.ABUSE_DETECTED,
            Actor.SYSTEM,
            AttemptOutcome.APPLIED,
            None,
        ),
        (
            State.BLOCKED,
            State.BLOCKED,
            Event.SUPERVISOR_UNBLOCK,
            Actor.OPERATOR,
            AttemptOutcome.REJECTED,
            "unauthorized_actor",
        ),
        (
            State.BLOCKED,
            State.BLOCKED,
            Event.OPERATOR_RELEASE,
            Actor.OPERATOR,
            AttemptOutcome.REJECTED,
            "unauthorized_actor",
        ),
        (
            State.BLOCKED,
            State.AUTO,
            Event.SUPERVISOR_UNBLOCK,
            Actor.SUPERVISOR,
            AttemptOutcome.APPLIED,
            None,
        ),
    ]
    assert world.stored().abuse_strikes == 0  # the supervisor's review spends the strikes


def test_3_abuse_while_a_person_answers_reaches_the_operator_inbox_and_the_operator_blocks(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    world.team.takeover(CID, Actor.OPERATOR)
    world.say("idiota")
    world.say("idiota")

    assert world.stored().control_state is State.HUMAN
    assert [(n.code, n.at) for n in world.stored().inbox] == [("abuse_detected", OPEN)]

    blocked = world.team.block(CID, Actor.OPERATOR)

    assert blocked.control_state is State.BLOCKED
    assert blocked.outbound == ()


# --------------------------------------------------------------------------- 4: out of hours


def test_4_out_of_hours_a_takeover_is_refused_and_logged(tmp_path: Path) -> None:
    world = make_world(tmp_path, now=CLOSED)

    with pytest.raises(GuardRejected) as refused:
        world.team.takeover(CID, Actor.OPERATOR)

    assert refused.value.code == "out_of_hours"
    assert log_rows(world) == [
        (
            State.AUTO,
            State.AUTO,
            Event.OPERATOR_TAKEOVER,
            Actor.OPERATOR,
            AttemptOutcome.REJECTED,
            "out_of_hours",
        )
    ]


def test_4_out_of_hours_a_customer_asking_for_a_person_gets_the_window(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Te ayudo yo.")], now=CLOSED)

    result = world.say("quiero hablar con una persona")

    assert result.control_state is State.AUTO
    assert texts(result) == [f"Te ayudo yo.\n\n{human_unavailable(WINDOW)}"]
    assert "de lunes a viernes de 09:00 a 18:00, hora de Buenos Aires" in texts(result)[0]


def test_4_a_human_conversation_returns_to_the_agent_at_closing_time(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    world.team.takeover(CID, Actor.OPERATOR)
    world.clock.instant = CLOSED

    result = world.say("sí")

    assert result.events[0] == "out_of_hours_started:applied"
    assert result.control_state is State.AUTO
    assert texts(result) == [f"{nothing_pending(None)}\n\n{hours_notice(WINDOW)}"]
    assert world.rows == 0


def test_4_after_closing_an_operator_can_no_longer_write_to_the_customer(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.team.takeover(CID, Actor.OPERATOR)
    world.clock.instant = CLOSED

    with pytest.raises(OperatorReplyNotAllowed):
        world.team.operator_reply(CID, Actor.OPERATOR, "¿Seguís ahí?")

    # The command found the window closed and handed the conversation back first (on record),
    # then refused to write: nothing was delivered.
    assert world.stored().control_state is State.AUTO
    assert log_rows(world)[-1] == (
        State.HUMAN,
        State.AUTO,
        Event.OUT_OF_HOURS_STARTED,
        Actor.SYSTEM,
        AttemptOutcome.APPLIED,
        None,
    )
    assert all(m.role is not Role.ASSISTANT for m in world.stored().messages)


# --------------------------------------------------------------------------- 5: HUMAN


def test_5_operator_replies_reach_the_customer_and_the_model_after_a_release(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [text_reply("¿Algo más en lo que te ayude?")])
    world.team.takeover(CID, Actor.OPERATOR)
    world.say("tengo un problema con una entrada")

    sent = world.team.operator_reply(CID, Actor.OPERATOR, "Hola, soy Ana. Ya lo reviso.")
    world.team.release(CID, Actor.OPERATOR)
    world.say("gracias")

    assert sent.outbound == (Outbound("operator", "Hola, soy Ana. Ya lo reviso."),)
    operator_message = Message(
        Role.ASSISTANT, "Hola, soy Ana. Ya lo reviso.", author=Author.OPERATOR
    )
    (call,) = world.model.calls
    assert call.messages[1:] == (
        Message(Role.USER, "tengo un problema con una entrada"),
        operator_message,
        Message(Role.USER, "gracias"),
    )


def test_5_only_the_team_writes_to_the_customer_and_only_in_human(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    with pytest.raises(OperatorReplyNotAllowed):
        world.team.operator_reply(CID, Actor.OPERATOR, "Hola.")
    world.team.takeover(CID, Actor.OPERATOR)
    with pytest.raises(CommandNotAllowed):
        world.team.operator_reply(CID, Actor.CUSTOMER, "Hola.")
    with pytest.raises(CommandNotAllowed):
        world.team.operator_reply(CID, Actor.SYSTEM, "Hola.")

    assert all(m.role is not Role.ASSISTANT for m in world.stored().messages)


# --------------------------------------------------------------------------- 6: the race


def test_6_a_takeover_while_the_model_answers_drops_the_answer(tmp_path: Path) -> None:
    box: list[World] = []

    def operator_takes_over(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        box[0].team.takeover(CID, Actor.OPERATOR)
        return text_reply("Respuesta que ya nadie debe ver.")

    world = make_world(tmp_path, [text_reply("Hola."), operator_takes_over])
    box.append(world)
    world.say("hola")

    result = world.say("¿y ahora?")

    assert result.dropped == "control_changed"
    assert result.outbound == ()
    assert world.stored().control_state is State.HUMAN
    assert all(m.content != "Respuesta que ya nadie debe ver." for m in world.stored().messages)
    assert [m.content for m in world.stored().messages if m.role is Role.USER] == [
        "hola",
        "¿y ahora?",
    ]


# --------------------------------------------------------------------------- 9: the audit trail


def test_9_every_command_leaves_a_record_with_the_five_fields(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    commands = [
        (world.team.takeover, Actor.OPERATOR, None),
        (world.team.block, Actor.OPERATOR, None),
        (world.team.release, Actor.OPERATOR, UnauthorizedActor),
        (world.team.takeover, Actor.SUPERVISOR, InvalidTransition),
        (world.team.unblock, Actor.SUPERVISOR, None),
        (world.team.takeover, Actor.CUSTOMER, UnauthorizedActor),
        (world.team.release, Actor.OPERATOR, InvalidTransition),
    ]

    for command, actor, refusal in commands:
        before = len(world.stored().control_log) if world.store.load(CID) else 0
        if refusal is None:
            command(CID, actor)
        else:
            with pytest.raises(refusal):
                command(CID, actor)
        assert len(world.stored().control_log) == before + 1

    records = world.stored().control_log
    assert all(record.at == OPEN for record in records)
    assert log_rows(world) == [
        (
            State.AUTO,
            State.HUMAN,
            Event.OPERATOR_TAKEOVER,
            Actor.OPERATOR,
            AttemptOutcome.APPLIED,
            None,
        ),
        (
            State.HUMAN,
            State.BLOCKED,
            Event.OPERATOR_BLOCK,
            Actor.OPERATOR,
            AttemptOutcome.APPLIED,
            None,
        ),
        (
            State.BLOCKED,
            State.BLOCKED,
            Event.OPERATOR_RELEASE,
            Actor.OPERATOR,
            AttemptOutcome.REJECTED,
            "unauthorized_actor",
        ),
        (
            State.BLOCKED,
            State.BLOCKED,
            Event.OPERATOR_TAKEOVER,
            Actor.SUPERVISOR,
            AttemptOutcome.REJECTED,
            "invalid_transition",
        ),
        (
            State.BLOCKED,
            State.AUTO,
            Event.SUPERVISOR_UNBLOCK,
            Actor.SUPERVISOR,
            AttemptOutcome.APPLIED,
            None,
        ),
        (
            State.AUTO,
            State.AUTO,
            Event.OPERATOR_TAKEOVER,
            Actor.CUSTOMER,
            AttemptOutcome.REJECTED,
            "unauthorized_actor",
        ),
        (
            State.AUTO,
            State.AUTO,
            Event.OPERATOR_RELEASE,
            Actor.OPERATOR,
            AttemptOutcome.REJECTED,
            "invalid_transition",
        ),
    ]
