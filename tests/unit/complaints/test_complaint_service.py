from datetime import UTC, datetime

from merchant_assistant.complaints.models import (
    Complaint,
    ComplaintDraft,
    CreateOutcome,
    idempotency_key,
)
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.shared.clock import FixedClock

NOW = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
DRAFT = ComplaintDraft("Falta de entrada", "Me cobraron dos veces la misma entrada")


class InMemoryComplaints:
    """A repository that really stores complaints, keyed like the database does."""

    def __init__(self) -> None:
        self._by_key: dict[str, Complaint] = {}

    def create(
        self, draft: ComplaintDraft, *, conversation_id: str, key: str, now: datetime
    ) -> CreateOutcome:
        if key in self._by_key:
            return CreateOutcome(self._by_key[key], created=False)
        complaint = Complaint(f"REC-{len(self._by_key) + 1:06d}", draft, conversation_id, key, now)
        self._by_key[key] = complaint
        return CreateOutcome(complaint, created=True)

    def find_by_key(self, key: str) -> Complaint | None:
        return self._by_key.get(key)

    def count(self) -> int:
        return len(self._by_key)


def service() -> tuple[ComplaintService, InMemoryComplaints]:
    repository = InMemoryComplaints()
    return ComplaintService(repository, FixedClock(NOW)), repository


def test_registering_stores_the_complaint_with_its_key_and_the_clock_time() -> None:
    complaints, repository = service()

    outcome = complaints.register(DRAFT, "conv-1")

    assert outcome.created is True
    assert outcome.complaint == Complaint(
        "REC-000001", DRAFT, "conv-1", idempotency_key("conv-1", DRAFT), NOW
    )
    assert repository.count() == 1


def test_registering_the_same_complaint_again_returns_the_first_one() -> None:
    complaints, repository = service()
    first = complaints.register(DRAFT, "conv-1")

    again = complaints.register(ComplaintDraft("FALTA  de entrada", DRAFT.description), "conv-1")

    assert again.created is False
    assert again.complaint == first.complaint
    assert repository.count() == 1


def test_the_same_text_in_another_conversation_is_another_complaint() -> None:
    complaints, repository = service()

    complaints.register(DRAFT, "conv-1")
    other = complaints.register(DRAFT, "conv-2")

    assert (other.created, other.complaint.id) == (True, "REC-000002")
    assert repository.count() == 2


def test_find_returns_the_registered_complaint_and_none_for_an_unknown_one() -> None:
    complaints, _ = service()
    registered = complaints.register(DRAFT, "conv-1").complaint

    assert complaints.find(DRAFT, "conv-1") == registered
    assert complaints.find(DRAFT, "conv-2") is None
    assert complaints.find(ComplaintDraft("Otro motivo", DRAFT.description), "conv-1") is None
