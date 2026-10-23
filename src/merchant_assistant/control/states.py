"""The vocabulary of the control state machine: who, what happened, and what to do about it."""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    """Who is in control of a conversation."""

    AUTO = "AUTO"  # the agent answers on its own
    SUGGEST = "SUGGEST"  # the agent drafts, an operator approves before anything is sent
    HUMAN = "HUMAN"  # an operator answers; the agent stays silent
    BLOCKED = "BLOCKED"  # nobody answers until a supervisor steps in


class Event(StrEnum):
    OPERATOR_TAKEOVER = "operator_takeover"
    CUSTOMER_REQUESTS_HUMAN = "customer_requests_human"
    LOW_CONFIDENCE = "low_confidence"
    ABUSE_DETECTED = "abuse_detected"
    OPERATOR_RELEASE = "operator_release"
    SUPERVISOR_UNBLOCK = "supervisor_unblock"
    OUT_OF_HOURS_STARTED = "out_of_hours_started"
    # Not in the brief: the brief had two contradictory targets for abuse while in HUMAN.
    # The operator's decision to block gets its own event (see SPEC_REVIEW.md, F1).
    OPERATOR_BLOCK = "operator_block"


class Actor(StrEnum):
    CUSTOMER = "CUSTOMER"
    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"  # may do everything an OPERATOR may


class Effect(StrEnum):
    """Instructions for the application layer. The domain stays pure: it never executes them."""

    INVALIDATE_PENDING_ACTION = "invalidate_pending_action"
    DISCARD_UNAPPROVED_DRAFTS = "discard_unapproved_drafts"
    NOTIFY_OPERATOR = "notify_operator"
