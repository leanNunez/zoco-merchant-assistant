"""The `crear_reclamo` use case: derive the key and the timestamp, then store."""

from __future__ import annotations

from merchant_assistant.complaints.models import (
    Complaint,
    ComplaintDraft,
    CreateOutcome,
    idempotency_key,
)
from merchant_assistant.complaints.repository import ComplaintRepository
from merchant_assistant.shared.clock import Clock


class ComplaintService:
    def __init__(self, repository: ComplaintRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def register(self, draft: ComplaintDraft, conversation_id: str) -> CreateOutcome:
        return self._repository.create(
            draft,
            conversation_id=conversation_id,
            key=idempotency_key(conversation_id, draft),
            now=self._clock.now(),
        )

    def find(self, draft: ComplaintDraft, conversation_id: str) -> Complaint | None:
        return self._repository.find_by_key(idempotency_key(conversation_id, draft))
