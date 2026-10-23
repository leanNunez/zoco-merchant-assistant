"""The confirmation gate: an effect runs only on a confirmation of its exact parameters.

A model's call to an effect tool never writes anything. It becomes a `PendingAction`, bound
to a fingerprint of its tool and normalized arguments, and the user is shown exactly those
values. Only `decide`, reading the user's next message, can turn it into a `Confirmed`, and a
`Confirmed` is the only thing the application accepts to execute the effect.

Rules this module enforces:

- **Exact parameters.** The fingerprint is sensitive to case and punctuation, because the user
  confirmed the text they saw. Proposing again, with any change, supersedes the pending
  action: the older one can never be confirmed again.
- **A later turn.** `decide` reads only the user's text. A proposal made while the model is
  answering is new state for the next user message; there is no input through which the same
  turn that proposes could also confirm.
- **Strict yes.** Anything but a strictly affirmative reply (see `replies.py`) discards the
  pending action: a refusal cancels it, any other message invalidates it and the turn goes on
  normally. A later "sí" then finds nothing pending.
- **Once.** `mark_registered` clears the pending action and remembers the complaint reference,
  so repeating "sí" afterwards yields `NothingPending` with that reference.
- **Retry after a failure.** `decide` does not consume the pending action when it confirms.
  If the execution fails (or its outcome is unknown), the application simply does not call
  `mark_registered`; the action stays pending and a later "sí" yields a `Confirmed` with the
  same fingerprint. The retry is safe because the store is idempotent on the complaint.
- **A change of control.** `invalidate` drops the pending action; the application calls it
  when the control machine emits `INVALIDATE_PENDING_ACTION`.

Every function is pure: it takes a state and returns a new one with an outcome. Nothing reads
a clock (the proposal time is passed in) and no state is ever mutated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, NoReturn

from pydantic import BaseModel, ValidationError

from merchant_assistant.agent.domain.errors import (
    InvalidPendingAction,
    InvalidRegistration,
    StaleConfirmation,
)
from merchant_assistant.agent.domain.guards import ValidatedCall
from merchant_assistant.agent.domain.replies import ReplyKind, classify_reply
from merchant_assistant.agent.domain.tools import TOOL_SPECS, ToolKind


def fingerprint(tool: str, arguments: BaseModel) -> str:
    """SHA-256 of the canonical JSON of the tool name and its normalized arguments."""
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingAction:
    tool: str
    arguments: BaseModel
    fingerprint: str
    proposed_at: datetime

    def __post_init__(self) -> None:
        spec = TOOL_SPECS.get(self.tool)
        if spec is None or spec.kind is not ToolKind.EFFECT:
            raise InvalidPendingAction(f"not an effect tool: {self.tool[:40]!r}")
        if not isinstance(self.arguments, spec.args_model):
            raise InvalidPendingAction(f"arguments are not {spec.args_model.__name__}")
        # Re-validate: a model built with model_construct or model_copy(update=...) skips
        # validation, and an unvalidated value must never be shown or executed.
        try:
            revalidated = spec.args_model.model_validate(self.arguments.model_dump())
        except ValidationError:
            raise InvalidPendingAction("arguments do not pass their own contract") from None
        if revalidated != self.arguments:
            raise InvalidPendingAction("arguments are not in their normalized form")
        if self.fingerprint != fingerprint(self.tool, self.arguments):
            raise InvalidPendingAction("fingerprint does not match the arguments")
        if self.proposed_at.utcoffset() is None:
            raise InvalidPendingAction("proposed_at must be timezone-aware")

    @classmethod
    def create(cls, call: ValidatedCall, proposed_at: datetime) -> PendingAction:
        if call.spec.kind is not ToolKind.EFFECT:
            raise InvalidPendingAction(f"{call.spec.name} is not an effect tool")
        return cls(
            tool=call.spec.name,
            arguments=call.arguments,
            fingerprint=fingerprint(call.spec.name, call.arguments),
            proposed_at=proposed_at,
        )


@dataclass(frozen=True, slots=True)
class RegisteredRef:
    complaint_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PendingState:
    pending: PendingAction | None = None
    last_registered: RegisteredRef | None = None


# Only this module holds it: `Confirmed(action)` written anywhere else raises TypeError.
_GATE_PROOF: Final = object()


class Confirmed:
    """The user confirmed this exact action. Built only by `decide`."""

    __slots__ = ("action",)
    action: PendingAction

    def __init__(self, action: PendingAction, *, _proof: object = None) -> None:
        if _proof is not _GATE_PROOF:
            raise TypeError("a Confirmed is issued only by the confirmation gate")
        object.__setattr__(self, "action", action)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Confirmed is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Confirmed is immutable")

    def __reduce__(self) -> NoReturn:
        # No copy or pickle path may rebuild one around the proof.
        raise TypeError("a Confirmed cannot be copied or serialized")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Confirmed) and other.action == self.action

    def __hash__(self) -> int:
        return hash(self.action.fingerprint)

    def __repr__(self) -> str:
        return f"Confirmed(fingerprint={self.action.fingerprint[:12]})"


@dataclass(frozen=True, slots=True)
class Proposed:
    """A new action is pending. `superseded` is the one it replaced, if any."""

    action: PendingAction
    superseded: PendingAction | None


@dataclass(frozen=True, slots=True)
class Cancelled:
    """The user said no. Nothing will be executed."""

    action: PendingAction


@dataclass(frozen=True, slots=True)
class Invalidated:
    """The user said something else, or control changed hands. The action was discarded."""

    action: PendingAction


@dataclass(frozen=True, slots=True)
class NothingPending:
    """There is nothing to decide. Carries the last registration, to say so plainly."""

    last_registered: RegisteredRef | None


@dataclass(frozen=True, slots=True)
class Registered:
    ref: RegisteredRef


Decision = Confirmed | Cancelled | Invalidated | NothingPending


def propose(state: PendingState, action: PendingAction) -> tuple[PendingState, Proposed]:
    """Make `action` the one pending action. Whatever was pending before is superseded."""
    return replace(state, pending=action), Proposed(action=action, superseded=state.pending)


def decide(state: PendingState, user_text: str) -> tuple[PendingState, Decision]:
    """Read the user's next message against the pending action.

    A confirmation leaves the action pending on purpose: it is cleared by `mark_registered`
    once the effect is known to have happened, so a failed execution can be retried.
    """
    action = state.pending
    if action is None:
        return state, NothingPending(state.last_registered)
    match classify_reply(user_text):
        case ReplyKind.AFFIRMATIVE:
            return state, Confirmed(action, _proof=_GATE_PROOF)
        case ReplyKind.NEGATIVE:
            return replace(state, pending=None), Cancelled(action)
        case ReplyKind.OTHER:
            return replace(state, pending=None), Invalidated(action)


def mark_registered(
    state: PendingState, confirmed: Confirmed, complaint_id: str
) -> tuple[PendingState, Registered]:
    """The effect happened (created now or found already stored). Clear and remember it."""
    if not complaint_id.strip():
        raise InvalidRegistration("a registration needs the complaint identifier")
    if state.pending is None or state.pending.fingerprint != confirmed.action.fingerprint:
        raise StaleConfirmation("the confirmation is not for the action currently pending")
    ref = RegisteredRef(complaint_id=complaint_id, fingerprint=confirmed.action.fingerprint)
    return PendingState(pending=None, last_registered=ref), Registered(ref)


def invalidate(state: PendingState) -> tuple[PendingState, Invalidated | NothingPending]:
    """Drop the pending action, as a change of who controls the conversation requires."""
    if state.pending is None:
        return state, NothingPending(state.last_registered)
    return replace(state, pending=None), Invalidated(state.pending)
