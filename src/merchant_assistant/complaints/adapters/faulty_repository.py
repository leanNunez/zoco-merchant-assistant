"""A complaints repository that fails on demand, for the simulated-failure scenarios."""

from __future__ import annotations

from datetime import datetime

from merchant_assistant.complaints.models import Complaint, ComplaintDraft, CreateOutcome
from merchant_assistant.complaints.repository import ComplaintRepository
from merchant_assistant.shared.faults import (
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
)

SUPPORTED_FAULTS = frozenset(FaultMode)


class FaultyComplaintRepository:
    def __init__(self, inner: ComplaintRepository, injector: FaultInjector) -> None:
        self._inner = inner
        self._injector = injector

    def create(
        self, draft: ComplaintDraft, *, conversation_id: str, key: str, now: datetime
    ) -> CreateOutcome:
        fault = self._injector.next_fault(SUPPORTED_FAULTS)
        if fault is FaultMode.TIMEOUT:
            raise SimulatedTimeout("simulated timeout before writing the complaint")
        if fault is FaultMode.ERROR:
            raise SimulatedFailure("simulated failure before writing the complaint")
        outcome = self._inner.create(draft, conversation_id=conversation_id, key=key, now=now)
        if fault is FaultMode.LOST_ACK:
            # The write happened; only the answer is lost. The caller cannot tell this from a
            # timeout, so retrying with the same key must find the stored complaint.
            raise SimulatedTimeout("simulated lost acknowledgement after writing the complaint")
        return outcome

    def find_by_key(self, key: str) -> Complaint | None:
        return self._inner.find_by_key(key)

    def count(self) -> int:
        return self._inner.count()
