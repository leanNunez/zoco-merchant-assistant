"""What the user is told at each step of the confirmation gate. Plain Spanish, no markdown.

These texts are owned by the code, not written by the model: the confirmation question must
show exactly the values that will be registered, and the outcome of an effect must be stated
by what actually happened, not by the model's reading of it.

The reason and the description are embedded verbatim, as data. They are normalized, so they
never contain a line break: each labelled line holds exactly one value and a hostile value
cannot forge a line of the surrounding text.
"""

from __future__ import annotations

from merchant_assistant.agent.domain.errors import InvalidPendingAction
from merchant_assistant.agent.domain.pending import PendingAction
from merchant_assistant.agent.domain.tools import CrearReclamoArgs


def confirmation_request(action: PendingAction) -> str:
    arguments = action.arguments
    if not isinstance(arguments, CrearReclamoArgs):
        raise InvalidPendingAction(f"no confirmation text for {action.tool!r}")
    return (
        "Voy a registrar este reclamo:\n"
        f"Motivo: {arguments.motivo}\n"
        f"Descripción: {arguments.descripcion}\n"
        'Si es correcto, respondé "sí" para registrarlo. Si no, respondé "no" para cancelarlo.'
    )


def registered(complaint_id: str, *, created: bool) -> str:
    if created:
        return (
            f"Registré tu reclamo con el número {complaint_id}. Ese número confirma que el "
            "reclamo quedó registrado, no que el problema ya esté resuelto."
        )
    return (
        f"Ese reclamo ya estaba registrado con el número {complaint_id}. No se registró uno nuevo."
    )


def cancelled() -> str:
    return "Listo, no registré el reclamo."


def nothing_pending(last_registered_id: str | None) -> str:
    if last_registered_id is None:
        return "No tengo ningún reclamo esperando confirmación."
    return (
        "No tengo ningún reclamo esperando confirmación. El último que registré tiene el "
        f"número {last_registered_id}, y no se registró otro."
    )


def execution_failed() -> str:
    return (
        "Tuve un problema técnico y no pude confirmar si el reclamo quedó guardado. "
        'Si respondés "sí", lo intento de nuevo; no se va a registrar dos veces.'
    )


def unavailable_in_this_state() -> str:
    return "Eso no está disponible en este momento de la conversación."
