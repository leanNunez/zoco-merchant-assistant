"""What the console says: Spanish lines, and the one place that cleans text before printing it.

Anything the customer, the model or an operator typed can contain control characters. A
carriage return or an escape sequence would let a line overwrite the label in front of it
(`Asistente:` turned into `Operador:`) or clear the screen, so every printed line goes through
`clean_lines` and a multi-line message keeps its label only on the first line.
"""

from __future__ import annotations

import unicodedata

from merchant_assistant.control import State
from merchant_assistant.shared.errors import DomainError

UNEXPECTED = "Ocurrió un error inesperado. Probá de nuevo."
UNKNOWN_COMMAND = "No reconozco ese comando. Escribí /help para ver los disponibles."
BAD_PIN = "PIN incorrecto."
LOGIN_USAGE = "Uso: /login operator <pin>  o  /login supervisor <pin>"
OPERATOR_HINT = (
    "Estás en la vista de operador: lo que escribas acá no se envía como cliente. "
    "Usá /reply <texto> para escribirle al cliente o /logout para volver a su vista."
)
NO_CONVERSATION = "Todavía no hay una conversación."

CUSTOMER_HELP = (
    "Escribí tu mensaje y el asistente te responde.",
    "  /hours                       horario de atención humana",
    "  /login operator <pin>        vista de operador (simulador)",
    "  /login supervisor <pin>      vista de supervisor (simulador)",
    "  /help                        esta ayuda",
    "  /quit                        salir",
)
TEAM_HELP = (
    "Vista de {role}. Lo que escribas sin / no se envía como cliente.",
    "  /state                       estado de la conversación y últimos cambios de control",
    "  /log                         registro de control completo",
    "  /inbox                       avisos para el equipo",
    "  /drafts                      borradores esperando una decisión",
    "  /approve <id>                aprobar un borrador",
    "  /reject <id>                 rechazar un borrador",
    "  /takeover                    tomar la conversación (el agente calla)",
    "  /release                     devolver la conversación al agente",
    "  /block                       suspender la conversación",
    "  /reply <texto>               escribirle al cliente (solo si la tomaste)",
    "  /hours                       horario de atención humana",
    "  /logout                      volver a la vista de cliente",
    "  /quit                        salir",
)
SUPERVISOR_EXTRA = ("  /unblock                     levantar una suspensión (solo supervisor)",)

STATE_GLOSS = {
    State.AUTO: "AUTO (responde el agente)",
    State.SUGGEST: "SUGGEST (el agente redacta, el equipo aprueba)",
    State.HUMAN: "HUMAN (responde una persona)",
    State.BLOCKED: "BLOCKED (suspendida hasta que la revise un supervisor)",
}

REASONS = {
    "no_key": "sin OPENAI_API_KEY para embeber las consultas",
    "incomplete_cache": "la caché de embeddings está incompleta (make index la completa)",
    "complete": "caché de embeddings completa",
}

# Refusals by stable code. The English `user_message` of a DomainError is never printed.
REFUSALS = {
    "unauthorized_actor": "Tu rol no puede hacer eso.",
    "out_of_hours": "Eso solo se puede hacer en el horario de atención humana (ver /hours).",
    "invalid_transition": "Eso no se puede hacer en el estado actual de la conversación.",
    "command_not_allowed": "Eso solo lo puede hacer un operador o un supervisor.",
    "operator_reply_not_allowed": "Primero tomá la conversación con /takeover.",
    "invalid_operator_message": "El mensaje está vacío o es demasiado largo.",
    "unknown_draft": "No existe ese borrador. Mirá /drafts.",
    "draft_not_pending": "Ese borrador ya fue decidido o descartado.",
    "draft_not_approvable": "Ese borrador ya no se puede aprobar.",
    "concurrent_update": "La conversación cambió mientras se procesaba. Probá de nuevo.",
}

_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")


def refusal(error: DomainError) -> str:
    """A short Spanish line for a refusal of the team's commands: by code, never by message."""
    known = REFUSALS.get(error.code)
    if known is not None:
        return known
    return f"No se pudo completar la operación (código: {error.code})."


def clean_lines(text: str) -> list[str]:
    """The text as printable lines: no control characters, no bidirectional overrides."""
    lines = [
        "".join(
            " " if char == "\t" else char
            for char in line
            if char == "\t" or (unicodedata.category(char) != "Cc" and char not in _BIDI_CONTROLS)
        )
        for line in text.splitlines()
    ]
    return lines or [""]


def labelled(label: str, text: str) -> str:
    """`Label: text`; the following lines are indented, so text cannot forge another label."""
    first, *rest = clean_lines(text)
    indent = " " * (len(label) + 2)
    body = [f"{indent}{line}" if line else "" for line in rest]
    return "\n".join([f"{label}: {first}", *body])
