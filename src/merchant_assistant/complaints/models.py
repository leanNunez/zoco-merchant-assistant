"""A complaint and the key that makes registering it idempotent.

The same complaint, in the same conversation, must get the same key no matter how it is
typed again: a customer who repeats "sí" or retries after a failure resends the same
words, perhaps with different spacing or capitals. The key is computed from the normalized,
casefolded text; the stored text keeps the customer's own casing.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from merchant_assistant.complaints.errors import InvalidComplaint

REASON_LENGTH = (3, 80)
DESCRIPTION_LENGTH = (10, 1000)


def normalize_text(text: str) -> str:
    """NFKC, trimmed, and every run of whitespace collapsed to a single space."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


@dataclass(frozen=True, slots=True)
class ComplaintDraft:
    """What the customer confirmed. Both fields are stored normalized (casing untouched)."""

    reason: str
    description: str

    def __post_init__(self) -> None:
        reason = normalize_text(self.reason)
        description = normalize_text(self.description)
        _check("reason", reason, REASON_LENGTH)
        _check("description", description, DESCRIPTION_LENGTH)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "description", description)


def _check(field: str, value: str, bounds: tuple[int, int]) -> None:
    low, high = bounds
    if not low <= len(value) <= high:
        raise InvalidComplaint(f"{field} has {len(value)} characters, expected {low} to {high}")


def idempotency_key(conversation_id: str, draft: ComplaintDraft) -> str:
    """SHA-256 of the conversation and the casefolded text.

    The draft's fields contain no newline (whitespace was collapsed), so joining with a
    newline is unambiguous.
    """
    if not conversation_id.strip():
        raise ValueError("a complaint belongs to a conversation: conversation_id is blank")
    material = "\n".join((conversation_id, draft.reason.casefold(), draft.description.casefold()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Complaint:
    id: str
    draft: ComplaintDraft
    conversation_id: str
    key: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    """`created` is False when the complaint already existed: nothing new was written."""

    complaint: Complaint
    created: bool
