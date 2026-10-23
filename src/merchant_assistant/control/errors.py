"""Why a transition was refused.

Each error has a stable `code` and a safe `user_message`. The `detail` names the state,
event and actor for the logs only: it must not reach the customer or the LLM.
"""

from __future__ import annotations

from typing import ClassVar

from merchant_assistant.control.states import Actor, Event, State
from merchant_assistant.shared.errors import DomainError


class ControlError(DomainError):
    """Base of every refusal of the control machine."""

    def __init__(self, state: State, event: Event, actor: Actor) -> None:
        super().__init__(f"state={state} event={event} actor={actor}")


class InvalidTransition(ControlError):  # noqa: N818 - names fixed by the specification
    """The (state, event) pair is not defined by the specification."""

    code: ClassVar[str] = "invalid_transition"
    user_message: ClassVar[str] = "That change is not possible in the current conversation state."


class UnauthorizedActor(ControlError):  # noqa: N818 - names fixed by the specification
    """The pair is defined, but this actor may not fire it."""

    code: ClassVar[str] = "unauthorized_actor"
    user_message: ClassVar[str] = "You are not allowed to make that change."


class GuardRejected(ControlError):  # noqa: N818 - names fixed by the specification
    """The pair and the actor are fine, but the open-hours guard failed."""

    code: ClassVar[str] = "out_of_hours"
    user_message: ClassVar[str] = "That change is only possible during human attention hours."
