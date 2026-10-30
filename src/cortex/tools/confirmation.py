"""Exact-intent confirmation of a write (brief section 3). Pure: no I/O.

A confirmation is valid for one set of values: the fingerprint of the tool and its normalized
arguments, `operation_id` included. Any change of a value changes the fingerprint and needs a new
confirmation. The question shows the exact values and is written by the code, not by the model,
so the model cannot paraphrase what the user is agreeing to.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from cortex.memory.ledger import PendingAction, PendingStatus
from merchant_assistant.agent.domain.pending import fingerprint

__all__ = ("canonical_arguments", "confirmation_question", "fingerprint", "is_confirmed")


def canonical_arguments(arguments: BaseModel) -> str:
    return json.dumps(
        arguments.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def is_confirmed(pending: PendingAction | None, fingerprint_: str, control_version: int) -> bool:
    """Confirmed for these exact values, while the same party kept control of the conversation."""
    return (
        pending is not None
        and pending.status is PendingStatus.CONFIRMED
        and pending.fingerprint == fingerprint_
        and pending.control_version == control_version
    )


def confirmation_question(tool: str, arguments: BaseModel) -> str:
    # Each value between «» on its own line: the user sees where every value starts and ends.
    values = "\n".join(
        f"- {name}: «{_escaped(value)}»" for name, value in arguments.model_dump().items()
    )
    return (
        f"Para ejecutar {tool} necesito tu confirmación de estos datos exactos:\n{values}\n"
        "Respondé «sí» para confirmar o «no» para cancelar."
    )


def _escaped(value: object) -> str:
    """The value with the delimiters backslash-escaped, so no value can close its own «»."""
    text = str(value)
    for character in ("\\", "«", "»"):
        text = text.replace(character, "\\" + character)
    return text
