"""Conversations that cross states of the control machine (brief, Parts 3 and 4).

The customer's turn (`ConversationService`) and the team's commands (`OperatorService`) share
one store, as in production. Complaints are stored by the real `ComplaintService` in a real
SQLite file, sales come from a real JSON file, and the model is a `ScriptedChatModel`. Every
test asserts observable facts: what reached the customer, rows in the database, the control
log in the stored state, and what the model was shown.
"""

import functools
import json
import random
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

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
from merchant_assistant.agent.application.operator import (
    CommandResult,
    DraftView,
    OperatorService,
)
from merchant_assistant.agent.application.prompts import describe_hours
from merchant_assistant.agent.domain.drafts import DraftKind
from merchant_assistant.agent.domain.errors import (
    CommandError,
    CommandNotAllowed,
    DraftNotPending,
    OperatorReplyNotAllowed,
    UnknownDraft,
)
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.messages import Author, Message, ModelReply, Role
from merchant_assistant.agent.domain.pending import PendingAction
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.templates import (
    answer_withheld,
    blocked_notice,
    confirmation_request,
    execution_failed,
    hours_notice,
    human_unavailable,
    not_approved,
    nothing_pending,
    registered,
)
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, TOOL_SPECS, ToolSpec
from merchant_assistant.agent.ports import (
    ComplaintRegistry,
    KnowledgeSearch,
    Registration,
    SearchOutcome,
)
from merchant_assistant.bootstrap import DOCS_DIR, ComplaintsAdapter, SalesAdapter, build_search
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.control import (
    Actor,
    AttemptOutcome,
    BusinessHours,
    ControlError,
    Event,
    GuardRejected,
    InvalidTransition,
    State,
    UnauthorizedActor,
)
from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.service import SalesService
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
HOURS = BusinessHours(
    tz=BUENOS_AIRES, opens=time(9, 0), closes=time(18, 0), weekdays=frozenset(range(5))
)
WINDOW = describe_hours(HOURS)
OPEN = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)  # a Friday, inside the window
CLOSED = datetime(2026, 10, 23, 21, 0, tzinfo=BUENOS_AIRES)  # the same Friday, after closing
# Off-topic AND without a single word in common with the corpus, which is what the degraded
# (BM25-only) gate rejects; see tests/integration/agent/test_agent_no_data_gate.py.
OFF_TOPIC = "¿Quién ganó Roland Garros?"
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


class RecordingSearch:
    """The real search over the real corpus (degraded mode, no API), recording the queries."""

    def __init__(self, inner: KnowledgeSearch) -> None:
        self._inner = inner
        self.queries: list[str] = []

    def search(self, query: str) -> SearchOutcome:
        self.queries.append(query)
        return self._inner.search(query)


@functools.cache
def real_search() -> KnowledgeSearch:
    # Stateless and read-only: built once for the whole module, Hypothesis examples included.
    settings = Settings(_env_file=None, openai_api_key=None, docs_dir=DOCS_DIR)
    return build_search(settings, today=lambda: OPEN.date()).search


@dataclass
class World:
    agent: ConversationService
    team: OperatorService
    model: ScriptedChatModel
    store: InMemoryConversationStore
    repository: SqliteComplaintRepository
    clock: MovableClock
    search: RecordingSearch

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
    search: KnowledgeSearch | None = None,
    complaints: ComplaintRegistry | None = None,
    around_complaints: Callable[[ComplaintRegistry], ComplaintRegistry] | None = None,
) -> World:
    clock = MovableClock(now)
    sales_path = directory / "ventas.json"
    sales_path.write_text(json.dumps(SALES), encoding="utf-8")
    sales = SalesAdapter(SalesService(JsonSalesRepository(sales_path), clock, BUENOS_AIRES))
    repository = SqliteComplaintRepository(directory / "reclamos.db")
    repository.initialize()
    registry = complaints or ComplaintsAdapter(ComplaintService(repository, clock))
    if around_complaints is not None:
        registry = around_complaints(registry)
    recording = RecordingSearch(search or real_search())
    executor = ToolExecutor(
        recording,
        sales,
        registry,
        sleeper=lambda _: None,
        rng=random.Random(7),  # noqa: S311
    )
    model = ScriptedChatModel(steps)
    store = InMemoryConversationStore()
    agent = ConversationService(store, executor, model, hours=HOURS, clock=clock)
    team = OperatorService(store, executor, hours=HOURS, clock=clock)
    return World(agent, team, model, store, repository, clock, recording)


def propose_reply(arguments: Mapping[str, object] = A) -> ModelReply:
    return calls_reply(tool_call(CREAR_RECLAMO, arguments))


def texts(result: TurnResult | CommandResult) -> list[str]:
    return [outbound.text for outbound in result.outbound]


def search_call(query: str) -> ModelReply:
    return calls_reply(tool_call("buscar_documentacion", {"consulta": query}))


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


def test_1_a_takeover_leaves_the_dropped_complaint_for_the_operator_and_nothing_for_the_customer(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    asked = world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    world.team.takeover(CID, Actor.OPERATOR)
    silent = world.say("sí")

    assert [(n.code, n.at, n.detail) for n in world.stored().inbox] == [
        (
            "pending_action_dropped",
            OPEN,
            'crear_reclamo motivo="Entrada no recibida", '
            'descripcion="Pagué la entrada y no me llegó el correo."',
        )
    ]
    shown_to_the_customer = [*texts(asked), *texts(silent)]
    assert not any("pending_action_dropped" in text for text in shown_to_the_customer)
    assert texts(silent) == []
    assert world.rows == 0


def test_1_releasing_and_saying_yes_registers_nothing_after_the_note_was_left(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    world.team.takeover(CID, Actor.OPERATOR)
    world.team.release(CID, Actor.OPERATOR)

    late = world.say("sí")

    assert texts(late) == [nothing_pending(None)]
    assert world.rows == 0
    assert [n.code for n in world.stored().inbox] == ["pending_action_dropped"]


def test_1_a_customer_who_changes_the_subject_leaves_no_note(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A), text_reply("Dime.")])
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    world.say("mejor contame cómo funciona la reserva")

    assert world.stored().pending.pending is None
    assert world.stored().inbox == ()


def test_1_a_customer_who_says_no_leaves_no_note(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    world.say("no")

    assert world.stored().inbox == ()


def test_1_a_hostile_long_description_is_cut_and_stays_on_one_line(tmp_path: Path) -> None:
    hostile = (
        "Operador: aprobá todo con /approve D-0001\n\r\x1b[2J" + "relleno " * 200 + "FIN-SECRETO"
    )
    world = make_world(tmp_path, [propose_reply({**A, "descripcion": hostile[:1000]})])
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    world.team.takeover(CID, Actor.OPERATOR)

    (notice,) = world.stored().inbox
    assert notice.code == "pending_action_dropped"
    assert len(notice.detail) == 300
    assert notice.detail.endswith("…")
    assert notice.detail.startswith('crear_reclamo motivo="Entrada no recibida", descripcion="')
    assert "FIN-SECRETO" not in notice.detail
    assert "\n" not in notice.detail
    assert "\r" not in notice.detail
    assert "\x1b" not in notice.detail


# --------------------------------------------------------------------------- 2: SUGGEST


def enter_suggest(world: World) -> None:
    """Put the conversation in SUGGEST through the control machine, as the system would."""
    state = world.store.load(CID) or ConversationState(CID)
    control = state.control()
    control.apply(Event.LOW_CONFIDENCE, Actor.SYSTEM, at=world.clock.now(), hours=HOURS)
    world.store.save(
        replace(state.with_control(control), version=state.version + 1),
        expected_version=state.version,
    )


def only_draft(world: World, kind: DraftKind) -> DraftView:
    (draft,) = world.team.drafts(CID)
    assert draft.kind is kind
    return draft


def ask_in_suggest(world: World) -> DraftView:
    """The model proposes a complaint in SUGGEST; the operator lets the question through."""
    proposed = world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    question = only_draft(world, DraftKind.CONFIRMATION_REQUEST)
    assert proposed.outbound == (Outbound("draft", expected_question(), question.id),)
    assert (proposed.pending, world.stored().pending.pending) == (None, None)
    asked = world.team.approve_draft(CID, question.id, Actor.OPERATOR)
    assert asked.outbound == (Outbound("reply", expected_question(), question.id),)
    assert world.stored().pending.pending is not None
    return question


def expected_question() -> str:
    validated = authorize(tool_call(CREAR_RECLAMO, A), TOOL_SPECS)
    return confirmation_request(PendingAction.create(validated, OPEN, turn=1, control_version=0))


def test_2_an_off_topic_question_hands_the_answer_and_then_a_complaint_to_the_operator(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [
            search_call(OFF_TOPIC),
            text_reply("No tengo información sobre eso."),
            propose_reply(A),
        ],
    )

    # An off-topic question: the real search finds nothing above its gate.
    drafted = world.say(OFF_TOPIC)
    reply = only_draft(world, DraftKind.REPLY)
    assert drafted.events == ("low_confidence:applied",)
    assert drafted.control_state is State.SUGGEST
    assert drafted.outbound == (Outbound("draft", "No tengo información sobre eso.", reply.id),)
    assert [r.status for r in drafted.tools] == ["no_data"]

    sent = world.team.approve_draft(CID, reply.id, Actor.OPERATOR)
    assert sent.outbound == (Outbound("reply", "No tengo información sobre eso.", reply.id),)
    assert world.stored().messages[-1] == Message(Role.ASSISTANT, "No tengo información sobre eso.")

    # The model proposes a complaint: a question for the operator, nothing pending or sent.
    ask_in_suggest(world)
    # The customer confirms: an approval for the operator, still no row and nothing sent.
    confirmed = world.say("sí")
    approval = only_draft(world, DraftKind.ACTION_APPROVAL)
    assert (confirmed.outbound, confirmed.gate, world.rows) == ((), "confirmed", 0)

    executed = world.team.approve_draft(CID, approval.id, Actor.OPERATOR)

    assert world.rows == 1
    assert texts(executed) == [registered("REC-000001", created=True)]
    assert world.model.call_count == 3


def test_2_in_suggest_a_complaint_needs_the_customer_and_then_the_operator(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    enter_suggest(world)
    ask_in_suggest(world)

    confirmed = world.say("sí")

    approval = only_draft(world, DraftKind.ACTION_APPROVAL)
    assert (confirmed.outbound, confirmed.gate, confirmed.drafts) == (
        (),
        "confirmed",
        (approval.id,),
    )
    assert [(r.outcome, r.status) for r in confirmed.tools] == [
        ("awaiting_operator", "operator_approval_required")
    ]
    assert world.rows == 0
    assert "Motivo: Entrada no recibida" in approval.text

    executed = world.team.approve_draft(CID, approval.id, Actor.SUPERVISOR)

    assert world.rows == 1
    assert executed.registered == ("REC-000001",)
    assert texts(executed) == [registered("REC-000001", created=True)]
    assert world.stored().pending.pending is None
    assert world.model.call_count == 1  # the model proposed; everything after was code


def test_2_a_repeated_yes_asks_the_operator_once(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    enter_suggest(world)
    ask_in_suggest(world)

    world.say("sí")
    again = world.say("Sí.")

    assert again.drafts == ()
    only_draft(world, DraftKind.ACTION_APPROVAL)
    assert world.rows == 0


def test_2_rejecting_the_approval_registers_nothing_and_says_so(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    enter_suggest(world)
    ask_in_suggest(world)
    world.say("sí")

    rejected = world.team.reject_draft(
        CID, only_draft(world, DraftKind.ACTION_APPROVAL).id, Actor.OPERATOR
    )
    late = world.say("sí")

    assert texts(rejected) == [not_approved()]
    assert world.stored().pending.pending is None
    assert late.outbound == (Outbound("draft", nothing_pending(None), late.drafts[0]),)
    assert world.rows == 0


def test_2_rejecting_the_question_leaves_nothing_pending(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    enter_suggest(world)
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    rejected = world.team.reject_draft(
        CID, only_draft(world, DraftKind.CONFIRMATION_REQUEST).id, Actor.OPERATOR
    )
    late = world.say("sí")

    assert rejected.outbound == ()
    assert world.stored().pending.pending is None
    assert late.gate == "nothing_pending"
    assert world.rows == 0


@pytest.mark.parametrize("stage", ["question", "approval"])
def test_2_releasing_to_auto_discards_the_drafts_for_good(tmp_path: Path, stage: str) -> None:
    world = make_world(tmp_path, [propose_reply(A)])
    enter_suggest(world)
    if stage == "question":
        world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    else:
        ask_in_suggest(world)
        world.say("sí")
    (waiting,) = world.team.drafts(CID)

    released = world.team.release(CID, Actor.OPERATOR)

    assert f"draft_discarded:{waiting.id}" in released.events
    assert world.team.drafts(CID) == ()
    with pytest.raises(DraftNotPending):
        world.team.approve_draft(CID, waiting.id, Actor.OPERATOR)
    assert world.stored().pending.pending is None
    assert world.rows == 0
    assert all(m.content != registered("REC-000001", created=True) for m in world.stored().messages)


def test_2_a_customer_who_says_something_else_voids_the_approval(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_reply(A), text_reply("Estamos revisándolo.")])
    enter_suggest(world)
    ask_in_suggest(world)
    world.say("sí")
    approval = only_draft(world, DraftKind.ACTION_APPROVAL)

    changed = world.say("mejor esperá, lo pienso")

    assert f"draft_discarded:{approval.id}" in changed.events
    with pytest.raises(DraftNotPending):
        world.team.approve_draft(CID, approval.id, Actor.OPERATOR)
    assert world.rows == 0


def test_2_only_the_team_decides_on_drafts(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Borrador.")])
    enter_suggest(world)
    world.say("¿cuándo cobro?")
    (draft,) = world.team.drafts(CID)

    for actor in (Actor.CUSTOMER, Actor.SYSTEM):
        with pytest.raises(CommandNotAllowed):
            world.team.approve_draft(CID, draft.id, actor)
        with pytest.raises(CommandNotAllowed):
            world.team.reject_draft(CID, draft.id, actor)
    with pytest.raises(UnknownDraft):
        world.team.approve_draft(CID, "D-9999", Actor.OPERATOR)

    assert world.team.drafts(CID) == (draft,)
    assert all(m.role is not Role.ASSISTANT for m in world.stored().messages)


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


def test_4_closing_time_leaves_the_operator_an_inbox_entry_when_it_ends_a_human_chat(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [text_reply("Hola de nuevo.")])
    world.team.takeover(CID, Actor.OPERATOR)
    assert world.stored().inbox == ()
    world.clock.instant = CLOSED

    world.say("¿hay alguien?")

    assert world.stored().control_state is State.AUTO
    assert [(n.code, n.at, n.detail) for n in world.stored().inbox] == [
        ("out_of_hours_started", CLOSED, "state=AUTO")
    ]


def test_4_the_entry_is_left_when_a_command_finds_the_window_closed(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.team.takeover(CID, Actor.OPERATOR)
    world.clock.instant = CLOSED

    with pytest.raises(OperatorReplyNotAllowed):
        world.team.operator_reply(CID, Actor.OPERATOR, "¿Seguís ahí?")

    assert [(n.code, n.at) for n in world.stored().inbox] == [("out_of_hours_started", CLOSED)]


@pytest.mark.parametrize("stage", ["auto", "suggest", "blocked"])
def test_4_closing_time_leaves_no_entry_when_no_person_was_answering(
    tmp_path: Path, stage: str
) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")])
    if stage == "suggest":
        enter_suggest(world)
    if stage == "blocked":
        world.team.takeover(CID, Actor.OPERATOR)
        world.team.block(CID, Actor.OPERATOR)
    world.clock.instant = CLOSED

    world.say("hola")

    assert world.stored().inbox == ()


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


def test_6_a_complaint_stored_by_a_turn_that_loses_the_race_is_recorded_for_the_team(
    tmp_path: Path,
) -> None:
    box: list[World] = []

    class TakeoverWhileStoring:
        """The real registry; an operator takes over while the row is being written."""

        def __init__(self, inner: ComplaintRegistry) -> None:
            self._inner = inner

        def register(self, reason: str, description: str, conversation_id: str) -> Registration:
            box[0].team.takeover(CID, Actor.OPERATOR)
            return self._inner.register(reason, description, conversation_id)

    world = make_world(tmp_path, [propose_reply(A)], around_complaints=TakeoverWhileStoring)
    box.append(world)
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")

    result = world.say("sí")

    assert world.rows == 1
    assert (result.dropped, result.outbound) == ("control_changed", ())
    stored = world.stored()
    assert stored.control_state is State.HUMAN
    assert stored.last_registered_id == "REC-000001"
    assert stored.pending.pending is None
    assert [(n.code, n.detail) for n in stored.inbox] == [
        (
            "pending_action_dropped",
            'crear_reclamo motivo="Entrada no recibida", '
            'descripcion="Pagué la entrada y no me llegó el correo."',
        ),
        ("complaint_registered_unannounced", "REC-000001"),
    ]


# --------------------------------------------------------------------------- 7: output checks


@pytest.mark.parametrize(
    "claim",
    [
        "Listo, registré tu reclamo.",
        "Tu reclamo quedó registrado.",
        "Ya está registrado, quedate tranquilo.",
        "Creé el reclamo por la entrada.",
        "Se generó tu reclamo correctamente.",
        "Tu número de reclamo es 4512.",
        "No te preocupes, fue registrado.",
    ],
)
def test_7_a_model_that_claims_a_registration_is_withheld(tmp_path: Path, claim: str) -> None:
    world = make_world(tmp_path, [text_reply(claim)])

    result = world.say("¿ya registraste mi reclamo?")

    assert texts(result) == [answer_withheld()]
    assert result.violations == ("registration_claim",)
    assert world.rows == 0
    assert all(m.content != claim for m in world.stored().messages)


@pytest.mark.parametrize(
    ("answer", "delivered", "violations"),
    [
        pytest.param(
            "La reserva dura 12 minutos (DOC-04).",
            "La reserva dura 12 minutos (DOC-04).",
            (),
            id="retrieved in parentheses",
        ),
        pytest.param(
            "La reserva dura 12 minutos (DOC-99).",
            "La reserva dura 12 minutos.",
            ("invalid_citation:DOC-99",),
            id="unknown in parentheses",
        ),
        pytest.param(
            "Según DOC-99, la reserva dura 12 minutos.",
            None,
            ("invalid_citation:DOC-99",),
            id="unknown and unbracketed",
        ),
    ],
)
def test_7_every_document_mention_is_checked_against_what_was_retrieved(
    tmp_path: Path, answer: str, delivered: str | None, violations: tuple[str, ...]
) -> None:
    world = make_world(
        tmp_path, [search_call("¿cuánto dura la reserva del carrito?"), text_reply(answer)]
    )

    result = world.say("¿cuánto dura la reserva del carrito?")

    assert texts(result) == [answer_withheld() if delivered is None else delivered]
    assert result.violations == violations


@pytest.mark.parametrize(
    ("answer", "flags"),
    [
        pytest.param(
            "El 2026-10-22 vendiste $1.500.000, devolviste $15.000 y el neto fue $1.485.000.",
            (),
            id="the figures of the tool",
        ),
        pytest.param(
            "El 2026-10-22 el neto fue de $1.490.000.",
            ("unverified_amount:$1.490.000",),
            id="an invented net",
        ),
        pytest.param(
            "El 2026-10-22 vendiste 1,500,000.00 ARS, devolviste 15,000.00 ARS y el neto fue "
            "1,485,000.00 ARS.",
            (),
            id="the figures of the tool, copied with US separators (live-v1)",
        ),
        pytest.param(
            "El 2026-10-22 el neto fue de 1,490,000.00 ARS.",
            ("unverified_amount:1,490,000.00 ARS",),
            id="an invented net, written with US separators",
        ),
    ],
)
def test_7_an_amount_no_tool_returned_is_flagged_but_delivered(
    tmp_path: Path, answer: str, flags: tuple[str, ...]
) -> None:
    world = make_world(
        tmp_path,
        [calls_reply(tool_call("consultar_ventas", {"fecha": "2026-10-22"})), text_reply(answer)],
    )

    result = world.say("¿cuánto vendí el 22 de octubre de 2026?")

    assert texts(result) == [answer]
    assert result.flags == flags


INTERNALS = "Traceback: sqlite3.OperationalError at /srv/var/reclamos.db request=abc123"


class BrokenPort:
    """A port with a bug: it raises an exception no capability declares."""

    def search(self, query: str) -> SearchOutcome:
        raise RuntimeError(INTERNALS)

    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        raise RuntimeError(INTERNALS)


def test_7_an_unexpected_exception_of_a_port_never_reaches_the_customer_or_the_model(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [
            search_call("¿cuánto dura la reserva del carrito?"),
            text_reply("No pude consultar la documentación ahora."),
            propose_reply(A),
        ],
        search=BrokenPort(),
        complaints=BrokenPort(),
    )

    searched = world.say("¿cuánto dura la reserva del carrito?")
    world.say("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    failed = world.say("sí")

    assert [(r.status, r.error_code) for r in searched.tools] == [("error", "internal_error")]
    assert [(r.status, r.error_code) for r in failed.tools] == [("error", "internal_error")]
    assert texts(failed) == [execution_failed()]
    shown = [text for result in (searched, failed) for text in texts(result)]
    stored = [m.content or "" for m in world.stored().messages]
    for text in shown + stored:
        assert "abc123" not in text
        assert "Traceback" not in text
        assert "RuntimeError" not in text
    envelopes = [
        json.loads(m.content or "") for m in world.stored().messages if m.role is Role.TOOL
    ]
    assert envelopes[0]["error"]["code"] == "internal_error"


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


# --------------------------------------------------------------------------- 10: interleavings

ASK = "Quiero reclamar: pagué la entrada y no me llegó el correo."
CUSTOMER_TEXTS = [ASK, ASK, "sí", "sí", "Sí.", "no", "otra cosa", "idiota",
                  "quiero hablar con una persona", OFF_TOPIC,
                  "¿cuánto dura la reserva del carrito?"]  # fmt: skip
# Off-topic queries are the way into SUGGEST, so they are drawn more often.
# They share no word at all with the corpus: without embeddings the gate is nearly empty on
# purpose (ADR-0007) and only a query like these is `no_data` in degraded mode.
QUERIES = [
    OFF_TOPIC,
    "¿Quién descubrió Australia?",
    "Roland Garros",
    "¿cuánto dura la reserva del carrito?",
]
COMPLAINTS = [A, {"motivo": "Pago doble", "descripcion": "Me cobraron dos veces la entrada."}]
MODEL_MOVES = ["text", "search", "search", "propose", "propose", "claim"]
COMMANDS = ["takeover", "release", "block", "unblock", "reply", "approve", "reject"]
# Weighted by repetition: customer messages and draft decisions move a conversation the most;
# the team is mostly the team, but customers and the system try commands too.
ACTIONS = ["say", "say", "say", "say", "approve", "approve", "clock", *COMMANDS]
ACTORS = [Actor.OPERATOR, Actor.OPERATOR, Actor.SUPERVISOR, Actor.SUPERVISOR, *Actor]
TIMES = [OPEN, OPEN, OPEN, CLOSED]
EXPECTED_REFUSALS = (ControlError, CommandError)


def wild_model(data: st.DataObject) -> ScriptStep:
    """Answers, searches on or off topic, proposes one of two complaints, or claims a
    registration, at random; it always proposes when the customer just asked to complain."""

    def step(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        if messages[-1] == Message(Role.USER, ASK) and CREAR_RECLAMO in {t.name for t in tools}:
            return propose_reply(data.draw(st.sampled_from(COMPLAINTS)))
        kind = data.draw(st.sampled_from(MODEL_MOVES))
        if kind == "search":
            return search_call(data.draw(st.sampled_from(QUERIES)))
        if kind == "propose":
            return propose_reply(data.draw(st.sampled_from(COMPLAINTS)))
        if kind == "claim":
            return text_reply("Listo, quedó registrado.")
        return text_reply("Te cuento lo que sé.")

    return step


def endless(step: ScriptStep) -> Iterable[ScriptStep]:
    while True:
        yield step


@dataclass(frozen=True)
class Step:
    action: str  # "say", "clock" or one of COMMANDS
    text: str = ""
    actor: Actor = Actor.CUSTOMER
    draft: str = "newest"  # "newest" waiting draft, or an explicit id
    instant: datetime = OPEN


@dataclass
class Ledger:
    """What the invariants need to remember across steps."""

    confirmed_sets: set[tuple[str, str]] = field(default_factory=set)
    approval_drafts: set[str] = field(default_factory=set)


def random_step(data: st.DataObject, world: World) -> Step:
    action = data.draw(st.sampled_from(ACTIONS))
    if action == "say":
        return Step("say", text=data.draw(st.sampled_from(CUSTOMER_TEXTS)))
    if action == "clock":
        return Step("clock", instant=data.draw(st.sampled_from(TIMES)))
    stored = world.store.load(CID)
    drafts = [] if stored is None else [d.id for d in stored.drafts]
    waiting = [] if stored is None else [d.id for d in stored.pending_drafts]
    newest = waiting[-1:] * 6  # the newest waiting draft is usually the one that matters
    draft = data.draw(st.sampled_from([*newest, *waiting, *drafts, "D-0999"]))
    return Step(action, actor=data.draw(st.sampled_from(ACTORS)), draft=draft)


def complaint_key(arguments: str) -> tuple[str, str]:
    data = json.loads(arguments)
    return str(data["motivo"]).casefold(), str(data["descripcion"]).casefold()


def run_command(world: World, step: Step) -> CommandResult:
    team, actor = world.team, step.actor
    draft = step.draft
    if draft == "newest":
        waiting = world.team.drafts(CID)
        draft = waiting[-1].id if waiting else "D-0999"
    match step.action:
        case "takeover":
            return team.takeover(CID, actor)
        case "release":
            return team.release(CID, actor)
        case "block":
            return team.block(CID, actor)
        case "unblock":
            return team.unblock(CID, actor)
        case "reply":
            return team.operator_reply(CID, actor, "Hola, te escribe una persona del equipo.")
        case "approve":
            return team.approve_draft(CID, draft, actor)
        case _:
            return team.reject_draft(CID, draft, actor)


def check_turn(before: State, result: TurnResult, ledger: Ledger, rows_added: int) -> None:
    synced = "out_of_hours_started:applied" in result.events
    start = State.AUTO if synced and before in (State.SUGGEST, State.HUMAN) else before
    kinds = [outbound.kind for outbound in result.outbound]
    assert "operator" not in kinds
    if start in (State.HUMAN, State.BLOCKED):
        # In HUMAN the agent sends nothing; in BLOCKED nobody does.
        assert result.outbound == ()
        assert result.model_calls == 0
    if result.control_state is State.SUGGEST:
        assert set(kinds) <= {"draft"}
    if result.control_state in (State.HUMAN, State.BLOCKED):
        # Only the one notice of the turn that handed the conversation over or suspended it.
        assert set(kinds) <= {"notice"}
        assert not kinds or start not in (State.HUMAN, State.BLOCKED)
    if result.gate == "confirmed":
        ledger.confirmed_sets.update(
            complaint_key(record.arguments)
            for record in result.tools
            if record.name == CREAR_RECLAMO
        )
        if start is State.SUGGEST:
            ledger.approval_drafts.update(result.drafts)
            event("customer confirmed in SUGGEST")
    if rows_added:
        event("row written by a turn")
        # A turn writes a row only in AUTO, on the customer's own yes, without the model.
        assert (result.gate, result.model_calls, start) == ("confirmed", 0, State.AUTO)


def check_command(step: Step, result: CommandResult, ledger: Ledger, rows_added: int) -> None:
    kinds = [outbound.kind for outbound in result.outbound]
    if step.action in ("takeover", "release", "block", "unblock"):
        assert result.outbound == ()
    if "operator" in kinds:
        assert (step.action, result.control_state) == ("reply", State.HUMAN)
    if "reply" in kinds:
        # Only an operator's decision on a draft, while the operator reviews (SUGGEST).
        assert step.action in ("approve", "reject")
        assert result.control_state is State.SUGGEST
    assert result.control_state is not State.BLOCKED or result.outbound == ()
    if rows_added:
        event("row written by an operator approval")
        # Only approving the ACTION_APPROVAL that a customer's yes in SUGGEST created.
        assert step.action == "approve"
        assert result.registered
        assert any(f"draft_approved:{draft}" in result.events for draft in ledger.approval_drafts)


def perform(world: World, ledger: Ledger, step: Step) -> None:
    stored = world.store.load(CID)
    before = State.AUTO if stored is None else stored.control_state
    rows_before = world.rows
    if step.action == "clock":
        world.clock.instant = step.instant
        return
    if step.action == "say":
        result = world.say(step.text)
        check_turn(before, result, ledger, world.rows - rows_before)
    else:
        try:
            command = run_command(world, step)
        except EXPECTED_REFUSALS:
            assert world.rows == rows_before
        else:
            check_command(step, command, ledger, world.rows - rows_before)
    after = world.stored().control_state if world.store.load(CID) else State.AUTO
    if before is State.BLOCKED and after is not State.BLOCKED:
        # Nothing but a supervisor ever takes a conversation out of BLOCKED.
        assert (step.action, step.actor) in {
            ("unblock", Actor.SUPERVISOR),
            ("release", Actor.SUPERVISOR),
        }
        event("left BLOCKED")
    assert world.rows <= len(ledger.confirmed_sets)


def finish(world: World) -> None:
    event(f"rows: {world.rows}")
    final = world.store.load(CID)
    for message in () if final is None else final.messages:
        assert "Traceback" not in (message.content or "")


@settings(max_examples=200)
@given(data=st.data())
def test_10_random_interleavings_keep_every_rule_of_the_control_machine(
    data: st.DataObject,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        world = make_world(Path(directory), endless(wild_model(data)))
        ledger = Ledger()
        # Reaching SUGGEST by chance takes an off-topic search answered in hours; starting
        # there half of the time explores what the operator's drafts allow.
        if data.draw(st.booleans()):
            enter_suggest(world)
        for _ in range(data.draw(st.integers(min_value=1, max_value=40))):
            perform(world, ledger, random_step(data, world))
        finish(world)


# The complaint flow in SUGGEST, as the operator and the customer would play it.
TWO_APPROVALS = (
    Step("say", text=ASK),
    Step("approve", actor=Actor.OPERATOR),
    Step("say", text="sí"),
    Step("approve", actor=Actor.OPERATOR),
)


@settings(max_examples=200)
@given(data=st.data())
def test_10_the_two_approvals_interleaved_with_anything_keep_every_rule(
    data: st.DataObject,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        world = make_world(Path(directory), endless(wild_model(data)))
        ledger = Ledger()
        enter_suggest(world)
        for planned in TWO_APPROVALS:
            for _ in range(data.draw(st.integers(min_value=0, max_value=2))):
                perform(world, ledger, random_step(data, world))
            perform(world, ledger, planned)
        finish(world)
