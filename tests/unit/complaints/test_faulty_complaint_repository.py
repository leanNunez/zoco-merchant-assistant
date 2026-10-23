from datetime import UTC, datetime

import pytest

from merchant_assistant.complaints.adapters.faulty_repository import FaultyComplaintRepository
from merchant_assistant.complaints.models import Complaint, ComplaintDraft, CreateOutcome
from merchant_assistant.shared.faults import (
    FaultConfig,
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
)

NOW = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
DRAFT = ComplaintDraft("Falta de entrada", "Me cobraron dos veces la misma entrada")


class InMemoryComplaints:
    def __init__(self) -> None:
        self._by_key: dict[str, Complaint] = {}
        self.creates = 0

    def create(
        self, draft: ComplaintDraft, *, conversation_id: str, key: str, now: datetime
    ) -> CreateOutcome:
        self.creates += 1
        if key in self._by_key:
            return CreateOutcome(self._by_key[key], created=False)
        complaint = Complaint(f"REC-{len(self._by_key) + 1:06d}", draft, conversation_id, key, now)
        self._by_key[key] = complaint
        return CreateOutcome(complaint, created=True)

    def find_by_key(self, key: str) -> Complaint | None:
        return self._by_key.get(key)

    def count(self) -> int:
        return len(self._by_key)


def first_call_faults(mode: FaultMode) -> FaultInjector:
    # With seed 4 at rate 0.5 the first call faults and the second does not (checked against
    # random.Random(4): 0.236, then 0.721, with one extra draw for the mode in between).
    return FaultInjector(FaultConfig(rate=0.5, seed=4, modes=frozenset({mode})))


def test_a_timeout_raises_before_anything_is_written() -> None:
    inner = InMemoryComplaints()
    repository = FaultyComplaintRepository(inner, first_call_faults(FaultMode.TIMEOUT))

    with pytest.raises(SimulatedTimeout):
        repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    assert (inner.creates, inner.count()) == (0, 0)


def test_an_error_raises_before_anything_is_written() -> None:
    inner = InMemoryComplaints()
    repository = FaultyComplaintRepository(inner, first_call_faults(FaultMode.ERROR))

    with pytest.raises(SimulatedFailure):
        repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    assert (inner.creates, inner.count()) == (0, 0)


def test_a_lost_acknowledgement_raises_after_the_complaint_was_written() -> None:
    inner = InMemoryComplaints()
    repository = FaultyComplaintRepository(inner, first_call_faults(FaultMode.LOST_ACK))

    with pytest.raises(SimulatedTimeout, match="lost acknowledgement"):
        repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    assert inner.count() == 1


def test_retrying_after_a_lost_acknowledgement_finds_the_stored_complaint() -> None:
    inner = InMemoryComplaints()
    repository = FaultyComplaintRepository(inner, first_call_faults(FaultMode.LOST_ACK))
    with pytest.raises(SimulatedTimeout):
        repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    retry = repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    assert (retry.created, retry.complaint.id, inner.count()) == (False, "REC-000001", 1)


def test_reads_pass_through_untouched_and_do_not_consume_a_draw() -> None:
    inner = InMemoryComplaints()
    repository = FaultyComplaintRepository(inner, first_call_faults(FaultMode.TIMEOUT))

    assert repository.count() == 0
    assert repository.find_by_key("k") is None
    assert repository.count() == 0

    with pytest.raises(SimulatedTimeout):  # still the first draw
        repository.create(DRAFT, conversation_id="c", key="k", now=NOW)


def test_at_rate_one_the_reads_still_work() -> None:
    always = FaultInjector(FaultConfig(rate=1.0, seed=0, modes=frozenset(FaultMode)))
    repository = FaultyComplaintRepository(InMemoryComplaints(), always)

    assert (repository.count(), repository.find_by_key("k")) == (0, None)


def test_without_faults_create_behaves_like_the_inner_repository() -> None:
    repository = FaultyComplaintRepository(InMemoryComplaints(), FaultInjector.disabled())

    first = repository.create(DRAFT, conversation_id="c", key="k", now=NOW)
    second = repository.create(DRAFT, conversation_id="c", key="k", now=NOW)

    assert (first.created, second.created, second.complaint) == (True, False, first.complaint)
