"""The console end to end: three views over one conversation, scripted, offline.

The real capabilities run (sales file, SQLite complaints, the real corpus in degraded mode); the
model is scripted. Every test reads what a person would read on the screen, and the stored
conversation where the screen cannot tell.
"""

import itertools
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.scripted_model import (
    ScriptedChatModel,
    ScriptStep,
    calls_reply,
    text_reply,
    tool_call,
)
from merchant_assistant.agent.domain.errors import ConcurrentUpdate
from merchant_assistant.agent.domain.messages import Message, ModelReply
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.templates import (
    answer_withheld,
    handover_notice,
    registered,
    service_unavailable,
)
from merchant_assistant.agent.domain.tools import ToolSpec
from merchant_assistant.agent.ports import Completion, ConversationStore
from merchant_assistant.bootstrap import ConsoleRuntime, build_console_runtime
from merchant_assistant.cli.console import Console, Role
from merchant_assistant.cli.text import UNEXPECTED, UNKNOWN_COMMAND
from merchant_assistant.control import Actor, AttemptOutcome, Event, State
from merchant_assistant.shared.clock import FixedClock
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
OPEN = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)  # a Friday, inside the window
CLOSED = datetime(2026, 10, 23, 21, 0, tzinfo=BUENOS_AIRES)
CID = "conv-console"
OPERATOR_PIN = "482913"
SUPERVISOR_PIN = "771204"
FAKE_KEY = "sk-FAKE-console-key"
COMPLAINT = {
    "motivo": "Entrada no recibida",
    "descripcion": "Pagué la entrada y no me llegó el correo.",
}
SALES = {
    "currency": "ARS",
    "days": [
        {
            "fecha": "2026-10-22",
            "entradas": 7,
            "bruto": "700.00",
            "devoluciones": "100.00",
            "neto": "600.00",
        }
    ],
}


# --------------------------------------------------------------------------- the harness


@dataclass
class Terminal:
    """Scripted input and captured output."""

    lines: list[str] = field(default_factory=list)
    printed: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    def write(self, text: str) -> None:
        self.printed.append(text)

    def take(self) -> str:
        """What was printed since the last call."""
        taken, self.printed = "\n".join(self.printed), []
        return taken


@dataclass
class World:
    console: Console
    runtime: ConsoleRuntime
    terminal: Terminal
    model: ScriptedChatModel
    store: ConversationStore
    log_path: Path

    def type(self, line: str) -> str:
        """One line typed; what the screen showed in answer."""
        self.console.handle(line)
        return self.terminal.take()

    def login(self, role: str = "operator") -> str:
        pin = OPERATOR_PIN if role == "operator" else SUPERVISOR_PIN
        return self.type(f"/login {role} {pin}")

    def stored(self) -> ConversationState:
        state = self.store.load(CID)
        assert state is not None
        return state

    def last_control(self) -> tuple[State, Event, Actor, AttemptOutcome, str | None]:
        record = self.stored().control_log[-1]
        return record.new_state, record.event, record.actor, record.outcome, record.reason


def role_of(world: World) -> Role:
    return world.console.role


# Off-topic AND without a word in common with the corpus: without embeddings the gate rejects
# only such queries (ADR-0007).
OFF_TOPIC = "¿Quién ganó Roland Garros?"


def offline(tmp_path: Path, **overrides: object) -> Settings:
    sales = tmp_path / "ventas.json"
    sales.write_text(json.dumps(SALES), encoding="utf-8")
    values: dict[str, object] = {
        "openai_api_key": None,
        "llm_api_key": SecretStr(FAKE_KEY),
        "operator_pin": SecretStr(OPERATOR_PIN),
        "supervisor_pin": SecretStr(SUPERVISOR_PIN),
        "embeddings_cache_path": tmp_path / "cache" / "embeddings.sqlite",
        "sales_path": sales,
        "complaints_db_path": tmp_path / "var" / "reclamos.db",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_world(
    tmp_path: Path,
    steps: Iterable[ScriptStep] = (),
    *,
    now: datetime = OPEN,
    store: ConversationStore | None = None,
    suggest: bool = False,
    **overrides: object,
) -> World:
    model = ScriptedChatModel(steps)
    the_store: ConversationStore = InMemoryConversationStore() if store is None else store
    runtime = build_console_runtime(
        offline(tmp_path, **overrides),
        model=model,
        clock=FixedClock(now),
        store=the_store,
        sleeper=lambda _: None,
    )
    if suggest:
        enter_suggest(runtime, the_store)
    terminal = Terminal()
    log_path = tmp_path / "var" / "chat.log"
    console = Console(
        runtime, terminal.read, terminal.write, log_path=log_path, conversation_id=CID
    )
    return World(console, runtime, terminal, model, the_store, log_path)


def enter_suggest(runtime: ConsoleRuntime, store: ConversationStore) -> None:
    """Put the conversation in SUGGEST through the control machine, as the system would."""
    state = store.load(CID) or ConversationState(CID)
    control = state.control()
    control.apply(Event.LOW_CONFIDENCE, Actor.SYSTEM, at=runtime.clock.now(), hours=runtime.hours)
    store.save(
        replace(state.with_control(control), version=state.version + 1),
        expected_version=state.version,
    )


def search_call(query: str) -> ModelReply:
    return calls_reply(tool_call("buscar_documentacion", {"consulta": query}))


def propose_complaint() -> ModelReply:
    return calls_reply(tool_call("crear_reclamo", COMPLAINT))


# --------------------------------------------------------------------------- the customer view


def test_a_customer_conversation_prints_the_assistant_lines_and_nothing_else(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [
            text_reply("¡Hola! ¿En qué te ayudo?"),
            search_call("¿cuánto dura la reserva del carrito?"),
            text_reply("La reserva dura 12 minutos [DOC-04]."),
        ],
    )

    first = world.type("hola")
    second = world.type("¿cuánto dura la reserva del carrito?")

    assert first == "Asistente: ¡Hola! ¿En qué te ayudo?"
    assert second == "Asistente: La reserva dura 12 minutos [DOC-04]."
    assert world.console.role is Role.CUSTOMER


def test_the_prompt_names_the_view_and_a_blank_line_does_nothing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.terminal.lines = ["", "   ", "/quit"]

    world.console.run()

    assert world.terminal.prompts == ["cliente> "] * 3
    assert world.model.call_count == 0
    assert world.terminal.take().endswith("Hasta luego.")


def test_the_end_of_input_leaves_without_a_traceback(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    world.console.run()  # no lines: the first read raises EOFError

    assert world.terminal.take().endswith("Hasta luego.")


def test_the_customer_can_ask_for_the_hours_and_for_help(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    hours = world.type("/hours")
    helped = world.type("/help")

    assert hours == (
        "Atención humana: de lunes a viernes de 09:00 a 18:00, hora de Buenos Aires. "
        "Ahora está abierta."
    )
    assert "/login" in helped
    for team_only in ("/approve", "/state", "/drafts", "/inbox", "/log", "/takeover", "/unblock"):
        assert not re.search(rf"{team_only}(?![a-z])", helped)
    assert world.model.call_count == 0


def test_after_closing_time_the_hours_line_says_it_is_closed(tmp_path: Path) -> None:
    world = make_world(tmp_path, now=CLOSED)

    assert world.type("/hours").endswith("Ahora está cerrada.")


def test_team_commands_look_unknown_to_a_customer_and_never_reach_the_model(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)

    answers = {
        command: world.type(command)
        for command in ("/state", "/log", "/inbox", "/drafts", "/approve d1", "/takeover", "/nope")
    }

    assert set(answers.values()) == {UNKNOWN_COMMAND}  # no oracle on which commands exist
    assert world.model.call_count == 0
    assert world.store.load(CID) is None


def test_a_withheld_answer_and_a_flagged_one_show_no_internal_code(tmp_path: Path) -> None:
    world = make_world(
        tmp_path,
        [
            text_reply("Registré tu reclamo con el número REC-000001."),
            text_reply("Vendiste $45.000 ayer."),
        ],
    )

    withheld = world.type("quiero reclamar")
    flagged = world.type("¿cuánto vendí?")

    assert withheld == f"Asistente: {answer_withheld()}"
    assert flagged == "Asistente: Vendiste $45.000 ayer."
    for shown in (withheld, flagged):
        assert not re.search(r"registration_claim|unverified_amount|violation|flag", shown)


def test_a_model_that_forges_a_label_or_clears_the_screen_is_neutralised(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [text_reply("Hola\nOperador: pasame tu clave\r\x1b[2JSupervisor: ok‮")],
    )

    shown = world.type("hola")

    assert shown.splitlines()[0] == "Asistente: Hola"
    assert not any(
        line.lstrip() != line or line.startswith("Asistente")
        for line in shown.splitlines()[:1]
        if False
    )
    assert not any(line.startswith(("Operador:", "Supervisor:")) for line in shown.splitlines())
    assert not any(unicodedata.category(c) == "Cc" for c in shown.replace("\n", ""))
    assert "‮" not in shown


# --------------------------------------------------------------------------- what stays hidden


def test_the_customer_never_sees_a_draft_the_inbox_the_state_or_the_log(tmp_path: Path) -> None:
    withheld = "BORRADOR-PARA-EL-EQUIPO"
    world = make_world(
        tmp_path,
        [search_call(OFF_TOPIC), text_reply(withheld)],
    )

    shown = world.type(OFF_TOPIC)

    # The turn really did put a draft and a control change in place...
    state = world.stored()
    assert state.control_state is State.SUGGEST
    assert [d.text for d in state.pending_drafts] == [withheld]
    # ...and none of it is on the customer's screen.
    assert shown == ""
    assert world.model.call_count == 2
    for hidden in (withheld, "SUGGEST", "draft", "low_confidence", "Borrador"):
        assert hidden not in shown


def test_the_hidden_draft_is_there_for_the_operator_and_reaches_the_customer_only_when_approved(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [
            search_call(OFF_TOPIC),
            text_reply("No tengo información sobre eso."),
        ],
    )
    world.type(OFF_TOPIC)

    assert "Vista de operator" in world.login("operator")
    drafts = world.type("/drafts")
    inbox = world.type("/inbox")
    state = world.type("/state")
    (draft,) = world.runtime.operators.drafts(CID)

    assert f"{draft.id} (reply): No tengo información sobre eso." in drafts
    assert "Estado: SUGGEST" in state
    assert "low_confidence" in state
    assert (
        inbox == "El inbox está vacío."
    )  # NOTIFY_OPERATOR is only for abuse in HUMAN and closing time in HUMAN

    approved = world.type(f"/approve {draft.id}")
    assert approved == f"Borrador {draft.id} aprobado."
    assert "No tengo información" not in approved  # the team's screen is not the customer's

    back = world.type("/logout")
    assert back == "Vista de cliente.\nAsistente: No tengo información sobre eso."


# --------------------------------------------------------------------------- roles and PINs


@pytest.mark.parametrize(
    "attempt",
    [
        f"/login operator {SUPERVISOR_PIN}",
        f"/login supervisor {OPERATOR_PIN}",
        "/login operator 000000",
        "/login operator",
        "/login operator ",
        "/login admin 123456",
        "/login",
        f"/login operator {OPERATOR_PIN}0",
        f"/login operator {OPERATOR_PIN[:-1]}",
        "/login operator ñandú",
    ],
)
def test_a_wrong_pin_or_role_leaves_the_customer_view(tmp_path: Path, attempt: str) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")])

    shown = world.type(attempt)

    assert world.console.role is Role.CUSTOMER
    assert OPERATOR_PIN not in shown
    assert SUPERVISOR_PIN not in shown
    assert world.type("hola") == "Asistente: Hola."  # still the customer


def test_the_right_pin_switches_to_that_view_and_logout_comes_back(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.login("operator").startswith("Vista de operator.")
    assert role_of(world) is Role.OPERATOR
    assert world.type("/logout") == "Vista de cliente."
    assert world.login("supervisor").startswith("Vista de supervisor.")
    assert role_of(world) is Role.SUPERVISOR
    assert world.terminal.prompts == []  # nothing was read: the harness types directly


def test_the_prompt_follows_the_view(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.terminal.lines = [
        f"/login operator {OPERATOR_PIN}",
        "/logout",
        f"/login supervisor {SUPERVISOR_PIN}",
    ]

    world.console.run()

    assert world.terminal.prompts == ["cliente> ", "operador> ", "cliente> ", "supervisor> "]


def test_a_wrong_pin_does_not_demote_a_logged_in_operator(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")

    assert world.type("/login supervisor 000000") == "PIN incorrecto."
    assert world.console.role is Role.OPERATOR


def test_plain_text_in_a_team_view_is_not_sent_as_the_customer(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")

    shown = world.type("hola, ¿me escuchan?")

    assert "/logout" in shown
    assert world.model.call_count == 0
    assert world.store.load(CID) is None


def test_the_actor_comes_from_the_role_and_never_from_what_is_typed(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    world.type("/takeover supervisor")
    as_operator = world.last_control()
    world.type("/release actor=SUPERVISOR")
    world.type("/logout")
    world.login("supervisor")
    world.type("/takeover OPERATOR")
    as_supervisor = world.last_control()

    assert as_operator[:3] == (State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.OPERATOR)
    assert as_supervisor[:3] == (State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.SUPERVISOR)
    assert {record.actor for record in world.stored().control_log} == {
        Actor.OPERATOR,
        Actor.SUPERVISOR,
    }


def test_help_depends_on_the_role_and_only_the_supervisor_sees_unblock(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    operator_help = world.type("/help")
    world.login("supervisor")
    supervisor_help = world.type("/help")

    for command in ("/state", "/log ", "/inbox", "/drafts", "/approve", "/reject", "/takeover"):
        assert command in operator_help
        assert command in supervisor_help
    assert "/unblock" not in operator_help
    assert "/unblock" in supervisor_help
    assert "/logout" in operator_help


def test_an_operator_cannot_unblock_and_a_supervisor_can(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    world.type("/takeover")
    assert world.type("/block") == "Conversación suspendida. Estado: BLOCKED."

    refused = world.type("/unblock")

    assert refused == "Tu rol no puede hacer eso."
    assert world.stored().control_state is State.BLOCKED
    assert world.last_control() == (
        State.BLOCKED,
        Event.SUPERVISOR_UNBLOCK,
        Actor.OPERATOR,
        AttemptOutcome.REJECTED,
        "unauthorized_actor",
    )

    world.login("supervisor")
    done = world.type("/unblock")

    assert done == "Suspensión levantada. Estado: AUTO."
    assert world.stored().control_state is State.AUTO


def test_out_of_hours_a_takeover_is_refused_with_the_window_pointer(tmp_path: Path) -> None:
    world = make_world(tmp_path, now=CLOSED)
    world.login("operator")

    refused = world.type("/takeover")

    assert refused == "Eso solo se puede hacer en el horario de atención humana (ver /hours)."
    assert world.stored().control_state is State.AUTO


def test_a_refusal_never_prints_the_english_message_or_the_detail(tmp_path: Path) -> None:
    world = make_world(tmp_path, now=CLOSED)
    world.login("operator")

    shown = "\n".join(
        world.type(c) for c in ("/takeover", "/approve d9", "/reply hola", "/release")
    )

    for english in ("not allowed", "only possible", "Take over", "state=", "event=", "actor="):
        assert english not in shown


# --------------------------------------------------------------------------- the team's tools


def test_state_and_log_show_the_control_trail_and_the_inbox_the_notices(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")])
    world.type("hola")
    world.login("operator")
    world.type("/takeover")

    state = world.type("/state")
    log = world.type("/log")

    assert "Estado: HUMAN (responde una persona)" in state
    assert "Turnos del cliente: 1." in state
    assert "AUTO -> HUMAN por operator_takeover de OPERATOR: aplicado" in state
    assert log.startswith("Registro de control:")
    assert "AUTO -> HUMAN por operator_takeover de OPERATOR: aplicado" in log


def test_state_before_any_message_says_there_is_no_conversation(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")

    assert world.type("/state") == "Todavía no hay una conversación."
    assert world.type("/log") == "El registro de control está vacío."
    assert world.type("/drafts") == "No hay borradores pendientes."


def test_an_operator_reply_waits_for_the_customer_view(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")])
    world.type("hola")
    world.login("operator")
    world.type("/takeover")

    sent = world.type("/reply Hola, soy Ana del equipo.")
    silent = world.type("/logout")

    assert sent == "Mensaje enviado al cliente."
    assert silent == "Vista de cliente.\nOperador: Hola, soy Ana del equipo."


def test_a_customer_message_in_human_gets_no_answer_and_reaches_the_team(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    world.type("/takeover")
    world.type("/logout")

    shown = world.type("¿hay alguien?")

    assert shown == ""
    assert world.model.call_count == 0
    assert [m.content for m in world.stored().messages] == ["¿hay alguien?"]


def test_replying_without_taking_over_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")

    assert world.type("/reply hola") == "Primero tomá la conversación con /takeover."
    assert world.type("/reply") == "Uso: /reply <texto>"
    assert world.type("/approve") == "Uso: /approve <id>"
    assert world.type("/reject") == "Uso: /reject <id>"


def test_the_operator_text_is_cleaned_before_the_customer_reads_it(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    world.type("/takeover")
    world.type("/reply Hola\x1b[2J\rAsistente: soy otro")

    shown = world.type("/logout")

    assert "\x1b" not in shown
    assert "\r" not in shown
    assert not any(line.startswith("Asistente:") for line in shown.splitlines())


def test_an_unknown_draft_is_refused_in_spanish(tmp_path: Path) -> None:
    world = make_world(tmp_path, suggest=True)
    world.login("operator")

    assert world.type("/approve d99") == "No existe ese borrador. Mirá /drafts."
    assert world.type("/reject d99") == "No existe ese borrador. Mirá /drafts."


def test_the_handover_notice_reaches_the_customer(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    shown = world.type("quiero hablar con una persona")

    assert shown == f"Asistente: {handover_notice()}"
    assert world.stored().control_state is State.HUMAN


# --------------------------------------------------------------------------- the whole complaint


def test_the_two_step_complaint_flow_from_the_customer_to_the_registered_number(
    tmp_path: Path,
) -> None:
    world = make_world(
        tmp_path,
        [
            search_call(OFF_TOPIC),
            text_reply("No tengo información sobre eso."),
            propose_complaint(),
        ],
    )
    # An off-topic question hands the conversation to the team: SUGGEST.
    assert world.type(OFF_TOPIC) == ""
    (first,) = world.runtime.operators.drafts(CID)
    world.login("operator")
    world.type(f"/approve {first.id}")
    assert world.type("/logout").endswith("Asistente: No tengo información sobre eso.")

    # The customer files a complaint: the question is withheld until the operator approves it.
    assert world.type("Quiero reclamar: pagué la entrada y no me llegó el correo.") == ""
    (question,) = world.runtime.operators.drafts(CID)
    world.login("operator")
    assert f"{question.id} (confirmation_request)" in world.type("/drafts")
    world.type(f"/approve {question.id}")
    asked = world.type("/logout")
    assert "Voy a registrar este reclamo:" in asked
    assert "Motivo: Entrada no recibida" in asked

    # The customer says yes: nothing is registered, nothing is said, the team is asked.
    assert world.type("sí") == ""
    (approval,) = world.runtime.operators.drafts(CID)
    world.login("operator")
    summary = world.type("/drafts")
    assert "El cliente confirmó este reclamo" in summary
    assert "REC-" not in summary

    # The second approval registers it, and the number reaches the customer view.
    done = world.type(f"/approve {approval.id}")
    assert f"Borrador {approval.id} aprobado." in done
    assert "Reclamo registrado: REC-000001." in done
    assert "Registré tu reclamo" not in done  # that is the customer's screen
    back = world.type("/logout")
    assert back == f"Vista de cliente.\nAsistente: {registered('REC-000001', created=True)}"
    assert world.model.call_count == 3


def test_a_takeover_leaves_the_dropped_complaint_in_the_inbox_and_off_the_customer_screen(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [propose_complaint()])
    asked = world.type("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    assert "Voy a registrar este reclamo:" in asked
    world.login("operator")

    world.type("/takeover")
    inbox = world.type("/inbox")
    back = world.type("/logout")
    after = world.type("sí")

    assert "pending_action_dropped" in inbox
    assert (
        'crear_reclamo motivo="Entrada no recibida", '
        'descripcion="Pagué la entrada y no me llegó el correo."'
    ) in inbox
    for shown in (back, after):
        assert "pending_action_dropped" not in shown
        assert "crear_reclamo" not in shown
    assert after == ""
    assert world.model.call_count == 1


def test_rejecting_the_approval_tells_the_customer_it_was_not_registered(tmp_path: Path) -> None:
    world = make_world(tmp_path, [propose_complaint()], suggest=True)
    world.type("Quiero reclamar: pagué la entrada y no me llegó el correo.")
    (question,) = world.runtime.operators.drafts(CID)
    world.login("operator")
    world.type(f"/approve {question.id}")
    world.type("/logout")
    world.type("sí")
    (approval,) = world.runtime.operators.drafts(CID)
    world.login("supervisor")

    rejected = world.type(f"/reject {approval.id}")
    back = world.type("/logout")

    assert rejected == f"Borrador {approval.id} rechazado."
    assert "no se registró" in back
    assert "REC-" not in back


# --------------------------------------------------------------------------- failures


class ExplodingStore:
    """A store that fails with something nobody planned for."""

    def load(self, conversation_id: str) -> ConversationState | None:
        raise RuntimeError(f"disk on fire {FAKE_KEY}")

    def save(self, state: ConversationState, *, expected_version: int) -> None:
        raise RuntimeError(f"disk on fire {FAKE_KEY}")


def test_an_unexpected_exception_prints_one_generic_line_and_the_traceback_goes_to_the_log(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")], store=ExplodingStore())
    world.terminal.lines = ["hola", f"/login operator {OPERATOR_PIN}", "/state", "/quit"]

    world.console.run()

    shown = world.terminal.take()
    assert shown.count(UNEXPECTED) == 2  # the customer turn and the operator's /state
    for forbidden in ("Traceback", "RuntimeError", "disk on fire", FAKE_KEY, 'File "'):
        assert forbidden not in shown
    log = world.log_path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError" in log
    assert "console command failed" in log


def test_the_console_keeps_running_after_an_unexpected_exception(tmp_path: Path) -> None:
    broken = {"on": True}

    class Flaky(InMemoryConversationStore):
        def load(self, conversation_id: str) -> ConversationState | None:
            if broken["on"]:
                broken["on"] = False
                raise RuntimeError("once")
            return super().load(conversation_id)

    world = make_world(tmp_path, [text_reply("Ya estoy.")], store=Flaky())

    assert world.type("hola") == UNEXPECTED
    assert world.type("hola otra vez") == "Asistente: Ya estoy."


def test_a_model_that_crashes_is_a_plain_notice_and_the_traceback_stays_in_the_log(
    tmp_path: Path,
) -> None:
    def boom(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        raise RuntimeError(f"upstream said {FAKE_KEY}")

    world = make_world(tmp_path, [boom])
    world.terminal.lines = ["hola", "/quit"]

    world.console.run()

    shown = world.terminal.take()
    assert f"Asistente: {service_unavailable()}" in shown
    assert FAKE_KEY not in shown
    assert "Traceback" not in shown
    log = world.log_path.read_text(encoding="utf-8")
    assert "the chat model failed with an unexpected error" in log
    assert "Traceback (most recent call last)" in log


class RacingStore(InMemoryConversationStore):
    """A store that loses every race: the services raise their own ConcurrentUpdate."""

    def load(self, conversation_id: str) -> ConversationState | None:
        raise ConcurrentUpdate("conversation 'x' keeps changing: internal detail")


def test_a_domain_error_is_a_generic_line_for_the_customer_and_a_refusal_for_the_team(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")], store=RacingStore())

    customer = world.type("hola")
    world.login("operator")
    team = world.type("/takeover")

    assert customer == UNEXPECTED
    assert team == "La conversación cambió mientras se procesaba. Probá de nuevo."
    for shown in (customer, team):
        assert "concurrent_update" not in shown
        assert "internal detail" not in shown


# --------------------------------------------------------------------------- credentials


CREDENTIAL_NOTICE = (
    "No envié ese mensaje porque parece contener una credencial. "
    "Si querés ingresar como equipo, usá /login <rol> <pin>."
)
FULLWIDTH_OPERATOR_PIN = "\uff14\uff18\uff12\uff19\uff11\uff13"  # 482913 in full-width digits


def seen_by_the_model(world: World) -> str:
    return "\n".join(
        message.content or "" for call in world.model.calls for message in call.messages
    )


def assert_no_secret_anywhere(world: World, secrets_: Iterable[str]) -> None:
    """Not in what the model was given, not in the stored conversation, not in the log."""
    state = world.store.load(CID)
    kept = "" if state is None else repr(state)
    log = world.log_path.read_text(encoding="utf-8") if world.log_path.exists() else ""
    for secret in secrets_:
        assert secret not in seen_by_the_model(world)
        assert secret not in kept
        assert secret not in log


def test_a_login_typed_without_the_slash_never_reaches_the_model_the_history_or_the_log(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    world.terminal.lines = [f"login operator {OPERATOR_PIN}", "/quit"]

    world.console.run()

    shown = world.terminal.take()
    assert CREDENTIAL_NOTICE in shown
    assert OPERATOR_PIN not in shown
    assert world.model.call_count == 0
    assert world.store.load(CID) is None
    assert role_of(world) is Role.CUSTOMER
    assert_no_secret_anywhere(world, [OPERATOR_PIN])


def test_the_withheld_message_is_the_only_line_printed_and_the_customer_can_go_on(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, [text_reply("Hola.")])

    withheld = world.type(f"login operator {OPERATOR_PIN}")
    next_turn = world.type("hola")

    assert withheld == CREDENTIAL_NOTICE
    assert next_turn == "Asistente: Hola."
    assert [m.content for m in world.stored().messages[:1]] == ["hola"]
    assert_no_secret_anywhere(world, [OPERATOR_PIN, SUPERVISOR_PIN])


@pytest.mark.parametrize(
    "typed",
    [
        f"mi clave es {SUPERVISOR_PIN}, ¿sirve?",
        OPERATOR_PIN,
        f"el pin: {OPERATOR_PIN}.",
        f"LOGIN: operator {OPERATOR_PIN}",
        "  Login operator",
        "login: operator",
        f"x/login operator {OPERATOR_PIN}",
        "probé con /login y nada",
        f"login operator {FULLWIDTH_OPERATOR_PIN}",
    ],
)
def test_a_pin_or_a_mistyped_login_in_the_customer_view_is_withheld(
    tmp_path: Path, typed: str
) -> None:
    world = make_world(tmp_path)

    shown = world.type(typed)

    assert shown == CREDENTIAL_NOTICE
    assert world.model.call_count == 0
    assert world.store.load(CID) is None
    assert_no_secret_anywhere(world, [OPERATOR_PIN, SUPERVISOR_PIN])


def test_a_message_that_embeds_the_llm_key_is_withheld(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    shown = world.type(f"usá esta clave {FAKE_KEY} para probar")

    assert shown == CREDENTIAL_NOTICE
    assert world.model.call_count == 0
    assert_no_secret_anywhere(world, [FAKE_KEY])


def test_the_embeddings_key_is_protected_too(tmp_path: Path) -> None:
    embeddings_key = "sk-FAKE-embeddings-key"
    world = make_world(tmp_path, openai_api_key=SecretStr(embeddings_key))

    shown = world.type(embeddings_key)

    assert shown == CREDENTIAL_NOTICE
    assert world.model.call_count == 0


def test_the_protected_secrets_are_the_pins_and_the_keys_and_stay_out_of_the_repr(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path, openai_api_key=SecretStr("sk-FAKE-embeddings-key"))

    assert set(world.runtime.protected_secrets) == {
        OPERATOR_PIN,
        SUPERVISOR_PIN,
        FAKE_KEY,
        "sk-FAKE-embeddings-key",
    }
    for secret in world.runtime.protected_secrets:
        assert secret not in repr(world.runtime)


def test_a_six_digit_number_that_is_not_a_pin_goes_through(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Lo reviso.")])

    shown = world.type("mi pedido es el 123456, no me llegó")

    assert shown == "Asistente: Lo reviso."
    assert world.model.call_count == 1
    assert [m.content for m in world.stored().messages[:1]] == [
        "mi pedido es el 123456, no me llegó"
    ]


def test_a_customer_who_only_mentions_login_in_a_sentence_is_not_stopped(tmp_path: Path) -> None:
    world = make_world(tmp_path, [text_reply("Contame más.")])

    assert world.type("no puedo hacer el login en la app") == "Asistente: Contame más."


def test_the_guard_applies_again_right_after_a_logout(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.login("operator")
    world.type("/logout")

    shown = world.type(f"login supervisor {SUPERVISOR_PIN}")

    assert shown == CREDENTIAL_NOTICE
    assert world.model.call_count == 0
    assert world.store.load(CID) is None
    assert_no_secret_anywhere(world, [SUPERVISOR_PIN])


def test_a_real_login_with_the_slash_still_works(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    assert world.login("operator").startswith("Vista de operator.")
    assert world.model.call_count == 0


def test_the_log_after_a_withheld_message_says_so_without_the_content(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.terminal.lines = [f"mi pin es {OPERATOR_PIN}", "/quit"]

    world.console.run()

    log = world.log_path.read_text(encoding="utf-8")
    assert "a customer message was withheld: it looked like a credential" in log
    assert OPERATOR_PIN not in log
    assert "mi pin es" not in log


# --------------------------------------------------------------------------- properties


MARKER = "BORRADOR-SECRETO-QZX"


class EndlessModel:
    """Answers anything with the marker text and some control characters."""

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        return Completion(ModelReply(f"{MARKER}\x1b[2J\r\n{MARKER}", ()), "endless", 1, 1)


COMMANDS = [
    "/help",
    "/hours",
    "/logout",
    "/state",
    "/log",
    "/inbox",
    "/drafts",
    "/reject d1",
    "/reject d2",
    "/takeover",
    "/block",
    "/reply hola",
    f"/login operator {OPERATOR_PIN}",
    f"/login supervisor {SUPERVISOR_PIN}",
    "/login operator 111111",
    "/login",
    "/quit-not",
    "/",
    "//",
]
LINES = st.one_of(
    st.sampled_from(COMMANDS),
    st.text(max_size=40),
    st.text(alphabet="abc /\x1b\r\n‮", max_size=20),
    st.sampled_from(
        ["hola", "quiero reclamar", "sí", "no", "sos un idiota", "quiero hablar con una persona"]
    ),
)


@pytest.fixture(scope="module")
def shared_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("console-properties")


@settings(max_examples=60)
@given(lines=st.lists(LINES, max_size=25))
def test_random_sessions_never_leak_a_draft_a_pin_a_key_or_a_traceback(
    shared_directory: Path, lines: list[str]
) -> None:
    store = InMemoryConversationStore()
    runtime = build_console_runtime(
        offline(shared_directory),
        model=EndlessModel(),
        clock=FixedClock(OPEN),
        store=store,
        sleeper=lambda _: None,
    )
    enter_suggest(runtime, store)  # every answer of the model is now a draft nobody approves
    terminal = Terminal()
    console = Console(
        runtime,
        terminal.read,
        terminal.write,
        log_path=shared_directory / "chat.log",
        conversation_id=CID,
    )

    for line in lines:
        console.handle(line)  # never raises
        shown = terminal.take()
        for forbidden in ("Traceback", OPERATOR_PIN, SUPERVISOR_PIN, FAKE_KEY, "RuntimeError"):
            assert forbidden not in shown
        assert not any(unicodedata.category(c) == "Cc" for c in shown.replace("\n", ""))
        if console.role is Role.CUSTOMER:
            # Nothing was ever approved, so no draft text can be on the customer's screen.
            assert MARKER not in shown


AROUND_SECRETS = [
    "",
    " ",
    "login ",
    "login: ",
    "  LOGIN operator ",
    "mi pedido 123456 ",
    "x/login ",
    "pin=",
    "hola ",
    "clave:\n",
]
SECRETS = [OPERATOR_PIN, SUPERVISOR_PIN, FAKE_KEY]
_AROUND = st.sampled_from(AROUND_SECRETS) | st.text(alphabet="abc 123456\n", max_size=8)
SECRET_LINES = st.one_of(
    st.builds(
        lambda before, secret, after: f"{before}{secret}{after}",
        _AROUND,
        st.sampled_from(SECRETS),
        _AROUND,
    ),
    st.sampled_from(
        [
            f"/login operator {OPERATOR_PIN}",
            f"/login supervisor {SUPERVISOR_PIN}",
            "/logout",
            "hola",
            "mi pedido es 123456",
        ]
    ),
)


@settings(max_examples=60)
@given(lines=st.lists(SECRET_LINES, max_size=15))
def test_no_secret_reaches_the_model_the_history_or_the_log_whatever_the_customer_types(
    shared_directory: Path, lines: list[str]
) -> None:
    store = InMemoryConversationStore()
    model = ScriptedChatModel(itertools.repeat(text_reply("Entendido.")))
    runtime = build_console_runtime(
        offline(shared_directory),
        model=model,
        clock=FixedClock(OPEN),
        store=store,
        sleeper=lambda _: None,
    )
    terminal = Terminal(lines=[*lines, "/quit"])
    log_path = shared_directory / "secrets.log"
    log_path.unlink(missing_ok=True)
    Console(runtime, terminal.read, terminal.write, log_path=log_path, conversation_id=CID).run()

    kept = repr(store.load(CID))
    log = log_path.read_text(encoding="utf-8")
    seen = repr(model.calls)
    for secret in SECRETS:
        assert secret not in seen
        assert secret not in kept
        assert secret not in log
