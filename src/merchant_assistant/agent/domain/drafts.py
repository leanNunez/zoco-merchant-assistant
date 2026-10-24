"""Drafts: what the agent would say or do in SUGGEST, waiting for an operator.

In SUGGEST nothing reaches the customer without an operator's approval (brief, Part 3), and a
complaint needs the operator's approval IN ADDITION to the customer's confirmation (rule 6).
Three kinds of draft carry that:

- `REPLY`: an answer of the agent. Approving it sends `text`, exactly.
- `CONFIRMATION_REQUEST`: the code-owned question for a complaint the model proposed, with the
  `PendingAction` it would ask about. Nothing is pending while the draft waits: the gate's
  promise ("the customer saw these exact values") only holds once the question is delivered,
  so the action is proposed when the operator approves the draft.
- `ACTION_APPROVAL`: the customer confirmed a delivered question; the draft holds the
  `Confirmed` the gate issued. Only approving it executes the complaint.

A draft is decided once. Leaving SUGGEST discards every pending draft (`discard_pending`): a
discarded draft is kept for the record and can never be approved or sent.

Every function is pure and returns new values; nothing reads a clock.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from merchant_assistant.agent.domain.errors import DraftNotPending, InvalidDraft, UnknownDraft
from merchant_assistant.agent.domain.pending import Confirmed, PendingAction
from merchant_assistant.control import Actor


class DraftKind(StrEnum):
    REPLY = "reply"
    CONFIRMATION_REQUEST = "confirmation_request"
    ACTION_APPROVAL = "action_approval"


class DraftStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISCARDED = "discarded"  # the conversation left SUGGEST before anyone decided


_DECIDERS = frozenset({Actor.OPERATOR, Actor.SUPERVISOR})


@dataclass(frozen=True, slots=True)
class Draft:
    """`text` is exactly what the customer would receive (for ACTION_APPROVAL: a summary for
    the operator, which is never sent)."""

    id: str
    kind: DraftKind
    text: str
    created_at: datetime
    created_turn: int
    status: DraftStatus = DraftStatus.PENDING
    action: PendingAction | None = None  # CONFIRMATION_REQUEST only
    confirmed: Confirmed | None = None  # ACTION_APPROVAL only
    decided_by: Actor | None = None
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.text.strip():
            raise InvalidDraft("a draft needs an identifier and a text")
        if self.created_at.utcoffset() is None:
            raise InvalidDraft("created_at must be timezone-aware")
        wants_action = self.kind is DraftKind.CONFIRMATION_REQUEST
        wants_confirmed = self.kind is DraftKind.ACTION_APPROVAL
        if (self.action is not None) != wants_action:
            raise InvalidDraft(
                f"a {self.kind} draft {'needs' if wants_action else 'has no'} action"
            )
        if (self.confirmed is not None) != wants_confirmed:
            raise InvalidDraft(
                f"a {self.kind} draft {'needs' if wants_confirmed else 'has no'} confirmation"
            )
        decided = self.decided_by is not None or self.decided_at is not None
        match self.status:
            case DraftStatus.PENDING:
                if decided:
                    raise InvalidDraft("a pending draft has no decision")
            case DraftStatus.APPROVED | DraftStatus.REJECTED:
                if self.decided_by not in _DECIDERS or self.decided_at is None:
                    raise InvalidDraft("only an operator or a supervisor decides on a draft")
            case DraftStatus.DISCARDED:
                if self.decided_by is not Actor.SYSTEM or self.decided_at is None:
                    raise InvalidDraft("a draft is discarded by the system, at a known time")

    @property
    def is_pending(self) -> bool:
        return self.status is DraftStatus.PENDING


def next_draft_id(drafts: tuple[Draft, ...]) -> str:
    """Identifiers are per conversation and never reused: D-0001, D-0002, ..."""
    return f"D-{len(drafts) + 1:04d}"


def pending_drafts(drafts: tuple[Draft, ...]) -> tuple[Draft, ...]:
    return tuple(draft for draft in drafts if draft.is_pending)


def find_pending(drafts: tuple[Draft, ...], draft_id: str) -> Draft:
    """The pending draft with this id; `UnknownDraft` or `DraftNotPending` otherwise."""
    for draft in drafts:
        if draft.id == draft_id:
            if not draft.is_pending:
                raise DraftNotPending(f"draft={draft.id} status={draft.status}")
            return draft
    raise UnknownDraft(f"draft={draft_id[:40]!r}")


def decide(
    drafts: tuple[Draft, ...],
    draft_id: str,
    status: DraftStatus,
    *,
    actor: Actor,
    at: datetime,
) -> tuple[Draft, ...]:
    """Approve or reject one pending draft. The others are left as they are."""
    if status not in (DraftStatus.APPROVED, DraftStatus.REJECTED):
        raise InvalidDraft("a decision approves or rejects")
    target = find_pending(drafts, draft_id)
    decided = replace(target, status=status, decided_by=actor, decided_at=at)
    return tuple(decided if draft.id == target.id else draft for draft in drafts)


def discard_pending(
    drafts: tuple[Draft, ...], *, at: datetime
) -> tuple[tuple[Draft, ...], tuple[str, ...]]:
    """Discard every pending draft, and say which ones. Decided drafts are kept as they are."""
    discarded: list[str] = []
    kept: list[Draft] = []
    for draft in drafts:
        if draft.is_pending:
            discarded.append(draft.id)
            draft = replace(
                draft, status=DraftStatus.DISCARDED, decided_by=Actor.SYSTEM, decided_at=at
            )
        kept.append(draft)
    return tuple(kept), tuple(discarded)
