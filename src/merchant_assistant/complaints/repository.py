"""The port through which complaints are stored."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from merchant_assistant.complaints.models import Complaint, ComplaintDraft, CreateOutcome


class ComplaintRepository(Protocol):
    def create(
        self, draft: ComplaintDraft, *, conversation_id: str, key: str, now: datetime
    ) -> CreateOutcome:
        """Store the complaint, or return the one already stored under `key`.

        Calling it again with the same key never writes a second complaint.
        """
        ...

    def find_by_key(self, key: str) -> Complaint | None: ...

    def count(self) -> int: ...
