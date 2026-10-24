"""The console: the customer, an operator and a supervisor over ONE running conversation.

The three roles are SEPARATE VIEWS, not three programs: they share a process because the
confirmation proof of a complaint lives in memory by design (ADR-0003). What each view shows:

- customer (default): only what the merchant's side has actually said to the customer: the
  agent's replies and notices, and what a person of the team wrote. Never a draft, the inbox,
  the state, the control log, a flag or a violation.
- operator and supervisor (after `/login`): the commands of the team, answered with what the
  services return. The `Actor` handed to the services comes ONLY from the logged-in role,
  never from anything typed.

The PIN is a role switch for a simulator, not authentication (ADR-0005). Every exception is
caught at the command boundary: the user sees one generic line and the traceback goes to the log
file. The console is the only place of the system that prints.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr

from merchant_assistant.agent.application.conversation import Outbound
from merchant_assistant.agent.application.prompts import describe_hours
from merchant_assistant.bootstrap import ConsoleRuntime
from merchant_assistant.cli import text
from merchant_assistant.cli.logs import DEFAULT_LOG_PATH, file_logging
from merchant_assistant.control import Actor, TransitionRecord
from merchant_assistant.shared.errors import DomainError

_LOGGER = logging.getLogger(__name__)

# What reaches the customer. A draft is NOT here: it is waiting for an operator.
_CUSTOMER_LABELS = {"reply": "Asistente", "notice": "Asistente", "operator": "Operador"}
RECENT_CONTROL_ENTRIES = 5
LOG_LIMIT = 50


class Role(StrEnum):
    CUSTOMER = "customer"
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"


_ACTORS = {Role.OPERATOR: Actor.OPERATOR, Role.SUPERVISOR: Actor.SUPERVISOR}
_PROMPTS = {
    Role.CUSTOMER: "cliente> ",
    Role.OPERATOR: "operador> ",
    Role.SUPERVISOR: "supervisor> ",
}
_LOGIN_ROLES = {"operator": Role.OPERATOR, "supervisor": Role.SUPERVISOR}


class Console:
    def __init__(
        self,
        runtime: ConsoleRuntime,
        input_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
        *,
        log_path: Path = DEFAULT_LOG_PATH,
        conversation_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._input = input_fn
        self._output = output_fn
        self._log_path = log_path
        # One conversation per run: complaints are idempotent per conversation, so a fixed id
        # would answer "already registered" to the same complaint typed in another run.
        self._conversation_id = conversation_id or f"consola-{uuid.uuid4().hex[:8]}"
        self._role = Role.CUSTOMER
        # What operators did that reaches the customer. It is shown when the view returns to
        # the customer's: the team's screen never doubles as the customer's.
        self._for_customer: list[Outbound] = []

    @property
    def role(self) -> Role:
        return self._role

    # ------------------------------------------------------------------ the loop

    def run(self) -> None:
        with file_logging(self._log_path):
            self._banner()
            while True:
                try:
                    line = self._input(_PROMPTS[self._role])
                except (EOFError, KeyboardInterrupt):
                    self._say("")
                    break
                try:
                    if not self.handle(line):
                        break
                except KeyboardInterrupt:
                    self._say("")
                    break
        self._say("Hasta luego.")

    def handle(self, line: str) -> bool:
        """One line of input. False when the user asked to leave. Never raises."""
        try:
            return self._dispatch(line.strip())
        except DomainError as refused:
            _LOGGER.warning("refused: %s %s", refused.code, refused.detail)
            self._say(text.refusal(refused) if self._role is not Role.CUSTOMER else text.UNEXPECTED)
        except Exception:
            _LOGGER.exception("console command failed")
            self._say(text.UNEXPECTED)
        return True

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, line: str) -> bool:
        if not line:
            return True
        if not line.startswith("/"):
            self._free_text(line)
            return True
        command, _, argument = line.partition(" ")
        name = command[1:].lower()
        argument = argument.strip()
        if name == "quit":
            return False
        handler = self._common().get(name) or (
            self._team().get(name) if self._role is not Role.CUSTOMER else None
        )
        if handler is None:
            self._say(text.UNKNOWN_COMMAND)
        else:
            handler(argument)
        return True

    def _common(self) -> dict[str, Callable[[str], None]]:
        return {
            "help": self._help,
            "hours": self._hours,
            "login": self._login,
            "logout": self._logout,
        }

    def _team(self) -> dict[str, Callable[[str], None]]:
        return {
            "state": self._state,
            "log": self._log,
            "inbox": self._inbox,
            "drafts": self._drafts,
            "approve": self._approve,
            "reject": self._reject,
            "takeover": self._takeover,
            "release": self._release,
            "block": self._block,
            "unblock": self._unblock,
            "reply": self._reply,
        }

    def _free_text(self, line: str) -> None:
        if self._role is not Role.CUSTOMER:
            self._say(text.OPERATOR_HINT)
            return
        result = self._runtime.service.handle_user_message(self._conversation_id, line)
        self._show_customer(result.outbound)

    # ------------------------------------------------------------------ common commands

    def _banner(self) -> None:
        self._say("ZocoTickets - asistente para comerciantes (simulador). Escribí /help.")
        shown = [
            (name, pin)
            for name, pin in (
                ("operator", self._runtime.operator_pin),
                ("supervisor", self._runtime.supervisor_pin),
            )
            if name in self._runtime.generated_pins
        ]
        if not shown:
            return
        self._say("")
        self._say("=== DEMO: esto es un simulador, no autenticación de producción ===")
        self._say("Los PIN son al azar, valen solo para esta sesión y se muestran una vez:")
        for name, pin in shown:
            self._say(f"  {name}: {pin.get_secret_value()}")
        self._say("Entrá con /login operator <pin> o /login supervisor <pin>.")
        self._say("En producción habría identidad por persona; acá cualquiera con el PIN entra.")
        self._say("===")

    def _help(self, argument: str) -> None:
        if self._role is Role.CUSTOMER:
            lines: Iterable[str] = text.CUSTOMER_HELP
        else:
            lines = [text.TEAM_HELP[0].format(role=self._role.value), *text.TEAM_HELP[1:]]
            if self._role is Role.SUPERVISOR:
                lines = [*lines[:-2], *text.SUPERVISOR_EXTRA, *lines[-2:]]
        self._say("\n".join(lines))

    def _hours(self, argument: str) -> None:
        hours = self._runtime.hours
        now = "abierta" if hours.is_open(self._runtime.clock.now()) else "cerrada"
        self._say(f"Atención humana: {describe_hours(hours)}. Ahora está {now}.")

    def _login(self, argument: str) -> None:
        role_name, _, pin = argument.partition(" ")
        role = _LOGIN_ROLES.get(role_name.lower())
        pin = pin.strip()
        if role is None or not pin:
            self._say(text.LOGIN_USAGE)
            return
        expected = (
            self._runtime.operator_pin if role is Role.OPERATOR else self._runtime.supervisor_pin
        )
        if not _same_pin(pin, expected):
            _LOGGER.warning("wrong PIN for the %s view", role.value)
            self._say(text.BAD_PIN)
            return
        self._role = role
        self._say(f"Vista de {role.value}. Escribí /help para ver los comandos.")

    def _logout(self, argument: str) -> None:
        if self._role is Role.CUSTOMER:
            self._say("Ya estás en la vista de cliente.")
            return
        self._role = Role.CUSTOMER
        self._say("Vista de cliente.")
        pending, self._for_customer = self._for_customer, []
        self._show_customer(pending)

    # ------------------------------------------------------------------ the team's commands

    def _actor(self) -> Actor:
        """Who acts: decided by the logged-in role alone, never by what was typed."""
        actor = _ACTORS.get(self._role)
        if actor is None:
            raise RuntimeError("a team command ran in the customer view")
        return actor

    def _state(self, argument: str) -> None:
        state = self._runtime.store.load(self._conversation_id)
        if state is None:
            self._say(text.NO_CONVERSATION)
            return
        waiting = "sí" if state.pending.pending is not None else "no"
        self._say(
            f"Estado: {text.STATE_GLOSS[state.control_state]}\n"
            f"Turnos del cliente: {state.turn}. Acción esperando confirmación: {waiting}. "
            f"Borradores pendientes: {len(state.pending_drafts)}. "
            f"Avisos en el inbox: {len(state.inbox)}."
        )
        recent = state.control_log[-RECENT_CONTROL_ENTRIES:]
        if recent:
            self._say("Últimos cambios de control:\n" + "\n".join(self._entry(e) for e in recent))

    def _log(self, argument: str) -> None:
        state = self._runtime.store.load(self._conversation_id)
        if state is None or not state.control_log:
            self._say("El registro de control está vacío.")
            return
        entries = state.control_log[-LOG_LIMIT:]
        self._say("Registro de control:\n" + "\n".join(self._entry(e) for e in entries))

    def _entry(self, record: TransitionRecord) -> str:
        at = record.at.astimezone(self._runtime.hours.tz)
        change = f"{record.previous_state} -> {record.new_state}"
        result = "aplicado" if record.outcome.value == "APPLIED" else f"rechazado ({record.reason})"
        return f"  {at:%Y-%m-%d %H:%M:%S} {change} por {record.event} de {record.actor}: {result}"

    def _inbox(self, argument: str) -> None:
        state = self._runtime.store.load(self._conversation_id)
        if state is None or not state.inbox:
            self._say("El inbox está vacío.")
            return
        zone = self._runtime.hours.tz
        rows = [
            f"  {notice.at.astimezone(zone):%H:%M:%S} {notice.code} {notice.detail}".rstrip()
            for notice in state.inbox
        ]
        self._say("Inbox:\n" + "\n".join(rows))

    def _drafts(self, argument: str) -> None:
        drafts = self._runtime.operators.drafts(self._conversation_id)
        if not drafts:
            self._say("No hay borradores pendientes.")
            return
        for draft in drafts:
            self._say(text.labelled(f"{draft.id} ({draft.kind.value})", draft.text))

    def _approve(self, argument: str) -> None:
        if not argument:
            self._say("Uso: /approve <id>")
            return
        result = self._runtime.operators.approve_draft(
            self._conversation_id, argument, self._actor()
        )
        self._say(f"Borrador {argument} aprobado.")
        self._deliver_to_customer(result.outbound)
        for complaint_id in result.registered:
            self._say(f"Reclamo registrado: {complaint_id}.")

    def _reject(self, argument: str) -> None:
        if not argument:
            self._say("Uso: /reject <id>")
            return
        result = self._runtime.operators.reject_draft(
            self._conversation_id, argument, self._actor()
        )
        self._say(f"Borrador {argument} rechazado.")
        self._deliver_to_customer(result.outbound)

    def _takeover(self, argument: str) -> None:
        result = self._runtime.operators.takeover(self._conversation_id, self._actor())
        self._say(f"Conversación tomada. Estado: {result.control_state}.")

    def _release(self, argument: str) -> None:
        result = self._runtime.operators.release(self._conversation_id, self._actor())
        self._say(f"Conversación devuelta al agente. Estado: {result.control_state}.")

    def _block(self, argument: str) -> None:
        result = self._runtime.operators.block(self._conversation_id, self._actor())
        self._say(f"Conversación suspendida. Estado: {result.control_state}.")

    def _unblock(self, argument: str) -> None:
        # Not gated here on purpose: the control machine is the authority on who may unblock,
        # and its refusal (recorded in the control log) is what an operator gets.
        result = self._runtime.operators.unblock(self._conversation_id, self._actor())
        self._say(f"Suspensión levantada. Estado: {result.control_state}.")

    def _reply(self, argument: str) -> None:
        if not argument:
            self._say("Uso: /reply <texto>")
            return
        result = self._runtime.operators.operator_reply(
            self._conversation_id, self._actor(), argument
        )
        self._say("Mensaje enviado al cliente.")
        self._deliver_to_customer(result.outbound)

    # ------------------------------------------------------------------ output

    def _deliver_to_customer(self, outbound: Sequence[Outbound]) -> None:
        """Queue what reached the customer; it is printed when the view becomes theirs."""
        self._for_customer.extend(outbound)

    def _show_customer(self, outbound: Sequence[Outbound]) -> None:
        for item in outbound:
            label = _CUSTOMER_LABELS.get(item.kind)
            if label is not None:  # anything else (a draft above all) is not the customer's
                self._say(text.labelled(label, item.text))

    def _say(self, message: str) -> None:
        self._output("\n".join(text.clean_lines(message)))


def _same_pin(typed: str, expected: SecretStr) -> bool:
    return secrets.compare_digest(typed.encode(), expected.get_secret_value().encode())
