"""What the agent may do in each state.

This module only states the policy. The agent layer must enforce it: offer the LLM only the
tools allowed here, and deliver the reply as this module says (ADR-0001: the LLM proposes,
the code disposes).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from merchant_assistant.control.states import State


class Delivery(StrEnum):
    SEND = "SEND"  # the reply goes to the customer
    DRAFT = "DRAFT"  # the reply waits for an operator's approval
    NONE = "NONE"  # no reply is generated


@dataclass(frozen=True)
class Capabilities:
    delivery: Delivery
    generate_reply: bool
    read_tools: bool  # documentation and sales lookups
    effect_tools: bool  # tools with side effects, such as filing a complaint
    # Brief rule 6: in SUGGEST a complaint needs the operator's approval in addition to the
    # customer's confirmation.
    effects_need_operator_approval: bool
    # Brief rule 3: out of hours only the AI answers, and it says when humans are available.
    inform_hours_window: bool


def capabilities_for(state: State, *, hours_open: bool) -> Capabilities:
    match state:
        case State.AUTO:
            delivery, generate, read, effect, approval = Delivery.SEND, True, True, True, False
        case State.SUGGEST:
            delivery, generate, read, effect, approval = Delivery.DRAFT, True, True, True, True
        case State.HUMAN:
            # Strict reading of "no actions": not even read tools. Classifying a message
            # with deterministic detectors is not acting, so it still runs.
            delivery, generate, read, effect, approval = Delivery.NONE, False, False, False, False
        case State.BLOCKED:
            delivery, generate, read, effect, approval = Delivery.NONE, False, False, False, False
    return Capabilities(
        delivery=delivery,
        generate_reply=generate,
        read_tools=read,
        effect_tools=effect,
        effects_need_operator_approval=approval,
        inform_hours_window=generate and not hours_open,
    )
