"""What the application does with the effects of an applied transition.

The control machine only DECLARES effects (`Outcome.effects`); this is the one place that
executes them, so the customer's turn and the operator's commands cannot drift apart:

- `INVALIDATE_PENDING_ACTION`: the half-confirmed complaint is dropped (the gate would also
  refuse it, because the control version changed: this makes it visible in the state). What
  the customer had been shown is left in the operator inbox as `pending_action_dropped`, so the
  person who takes over has the context. It is information, never an executable action. The
  model is told too, with a platform note in the history (`notes.py`): it made that proposal
  and would otherwise keep waiting for a "yes" that can no longer register anything.
- `DISCARD_UNAPPROVED_DRAFTS`: every pending draft is discarded. It stays in the record,
  marked DISCARDED, and can never be approved or sent.
- `NOTIFY_OPERATOR`: an entry in the operator inbox of the conversation.

Pure: the time is passed in, and the result names what happened so callers can report it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from merchant_assistant.agent.domain.drafts import discard_pending
from merchant_assistant.agent.domain.notes import Withdrawal, add_note
from merchant_assistant.agent.domain.pending import Invalidated, invalidate
from merchant_assistant.agent.domain.state import ConversationState, OperatorNotice
from merchant_assistant.control import Effect, Outcome

DROPPED_DETAIL_LIMIT = 300
_TRUNCATED = "…"


def dropped_detail(tool: str, arguments: Mapping[str, object]) -> str:
    """What the customer had been shown, as one short line for the operator inbox.

    The tool and its exact parameters, with every run of whitespace (line breaks too)
    collapsed, each value quoted so it cannot pass for the tool or another parameter, and the
    whole line cut to `DROPPED_DETAIL_LIMIT` characters. The values are the customer's words,
    so this is data to read, and the console prints it through its usual cleaning.
    """
    shown = ", ".join(
        f"{name}={json.dumps(' '.join(str(value).split()), ensure_ascii=False)}"
        for name, value in arguments.items()
    )
    line = f"{tool} {shown}".strip()
    if len(line) <= DROPPED_DETAIL_LIMIT:
        return line
    return line[: DROPPED_DETAIL_LIMIT - len(_TRUNCATED)] + _TRUNCATED


def apply_effects(
    state: ConversationState, outcome: Outcome, *, at: datetime
) -> tuple[ConversationState, tuple[str, ...]]:
    """The state after executing the outcome's effects, and one fact per executed effect."""
    facts: list[str] = []
    if Effect.INVALIDATE_PENDING_ACTION in outcome.effects:
        pending, dropped = invalidate(state.pending)
        state = replace(state, pending=pending)
        if isinstance(dropped, Invalidated):
            facts.append("pending_action_invalidated")
            action = dropped.action
            detail = dropped_detail(action.tool, action.arguments.model_dump())
            notice = OperatorNotice(at=at, code="pending_action_dropped", detail=detail)
            state = replace(state, inbox=(*state.inbox, notice))
            state = add_note(state, Withdrawal.DROPPED)
    if Effect.DISCARD_UNAPPROVED_DRAFTS in outcome.effects:
        drafts, discarded = discard_pending(state.drafts, at=at)
        state = replace(state, drafts=drafts)
        facts.append("discard_unapproved_drafts")
        facts.extend(f"draft_discarded:{draft_id}" for draft_id in discarded)
    if Effect.NOTIFY_OPERATOR in outcome.effects:
        notice = OperatorNotice(at=at, code=str(outcome.event), detail=f"state={outcome.new}")
        state = replace(state, inbox=(*state.inbox, notice))
        facts.append("notify_operator")
    return state, tuple(facts)
