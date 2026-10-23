import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from merchant_assistant.complaints.adapters.faulty_repository import FaultyComplaintRepository
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.errors import ComplaintStoreError
from merchant_assistant.complaints.models import ComplaintDraft, CreateOutcome, idempotency_key
from merchant_assistant.shared.faults import (
    FaultConfig,
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
DRAFT = ComplaintDraft("Falta de entrada", "Me cobraron dos veces la misma entrada")
OTHER = ComplaintDraft("Entrada duplicada", "Recibí dos códigos QR para la misma compra")


def create(
    repository: SqliteComplaintRepository,
    draft: ComplaintDraft = DRAFT,
    conversation_id: str = "conv-1",
) -> CreateOutcome:
    return repository.create(
        draft,
        conversation_id=conversation_id,
        key=idempotency_key(conversation_id, draft),
        now=NOW,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SqliteComplaintRepository:
    repository = SqliteComplaintRepository(tmp_path / "var" / "reclamos.db")
    repository.initialize()
    return repository


def test_the_first_complaint_is_rec_000001_and_the_next_is_rec_000002(
    repository: SqliteComplaintRepository,
) -> None:
    first = create(repository)
    second = create(repository, OTHER)

    assert (first.created, first.complaint.id) == (True, "REC-000001")
    assert (second.created, second.complaint.id) == (True, "REC-000002")


def test_the_stored_complaint_comes_back_complete(repository: SqliteComplaintRepository) -> None:
    created = create(repository)

    assert created.complaint.draft == DRAFT
    assert created.complaint.conversation_id == "conv-1"
    assert created.complaint.key == idempotency_key("conv-1", DRAFT)
    assert created.complaint.created_at == NOW
    assert repository.find_by_key(created.complaint.key) == created.complaint


def test_a_duplicate_key_returns_the_original_and_the_table_keeps_one_row(
    repository: SqliteComplaintRepository,
) -> None:
    first = create(repository)
    loud = ComplaintDraft("FALTA  DE ENTRADA", DRAFT.description.upper())
    later = repository.create(
        loud,
        conversation_id="conv-1",
        key=idempotency_key("conv-1", loud),
        now=NOW + timedelta(hours=1),
    )

    assert later.created is False
    assert later.complaint == first.complaint  # the original text, casing and timestamp
    assert repository.count() == 1


def test_a_duplicate_does_not_burn_an_id(repository: SqliteComplaintRepository) -> None:
    create(repository)
    create(repository)

    assert create(repository, OTHER).complaint.id == "REC-000002"


def test_the_same_text_in_another_conversation_is_another_complaint(
    repository: SqliteComplaintRepository,
) -> None:
    create(repository, conversation_id="conv-1")
    other = create(repository, conversation_id="conv-2")

    assert (other.created, other.complaint.id, repository.count()) == (True, "REC-000002", 2)


def test_the_text_keeps_the_users_casing(repository: SqliteComplaintRepository) -> None:
    draft = ComplaintDraft("Falta DE Entrada", "NO me llegó el QR")
    created = create(repository, draft)

    found = repository.find_by_key(created.complaint.key)

    assert found is not None
    assert (found.draft.reason, found.draft.description) == (
        "Falta DE Entrada",
        "NO me llegó el QR",
    )


def test_an_unknown_key_is_not_found(repository: SqliteComplaintRepository) -> None:
    assert repository.find_by_key("0" * 64) is None
    assert repository.count() == 0


def test_the_timestamp_is_stored_in_utc_whatever_the_offset_given(
    repository: SqliteComplaintRepository,
) -> None:
    buenos_aires = timezone(timedelta(hours=-3))
    local = datetime(2026, 10, 23, 12, 30, tzinfo=buenos_aires)

    created = repository.create(DRAFT, conversation_id="c", key="k", now=local)

    assert created.complaint.created_at == datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
    assert created.complaint.created_at.utcoffset() == timedelta(0)


def test_a_naive_timestamp_is_refused(repository: SqliteComplaintRepository) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.create(
            DRAFT,
            conversation_id="c",
            key="k",
            now=datetime(2026, 10, 23, 15, 30),  # noqa: DTZ001
        )

    assert repository.count() == 0


def test_ids_and_complaints_survive_a_new_instance_over_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "reclamos.db"
    first = SqliteComplaintRepository(path)
    first.initialize()
    create(first)

    second = SqliteComplaintRepository(path)

    assert second.count() == 1
    assert create(second).created is False
    assert create(second, OTHER).complaint.id == "REC-000002"


def test_the_database_itself_refuses_a_second_row_with_the_same_key(
    repository: SqliteComplaintRepository, tmp_path: Path
) -> None:
    # The guarantee is the UNIQUE constraint, not application code: bypass the repository
    # and the database still says no.
    create(repository)
    key = idempotency_key("conv-1", DRAFT)

    with (
        closing(sqlite3.connect(tmp_path / "var" / "reclamos.db")) as raw,
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
    ):
        raw.execute(
            "INSERT INTO complaints (idempotency_key, conversation_id, reason, description, "
            "created_at) VALUES (?, 'x', 'x', 'x', 'x')",
            (key,),
        )


def test_threads_racing_the_same_complaint_leave_exactly_one_row(tmp_path: Path) -> None:
    path = tmp_path / "reclamos.db"
    SqliteComplaintRepository(path).initialize()
    racers = 8

    for round_number in range(10):
        draft = ComplaintDraft(f"Reclamo {round_number}", "Me cobraron dos veces la misma entrada")
        start = threading.Barrier(racers)

        def race(draft: ComplaintDraft = draft, start: threading.Barrier = start) -> CreateOutcome:
            repository = SqliteComplaintRepository(path)  # one instance per thread
            start.wait()
            return create(repository, draft)

        with ThreadPoolExecutor(max_workers=racers) as pool:
            outcomes = [future.result() for future in [pool.submit(race) for _ in range(racers)]]

        assert sum(outcome.created for outcome in outcomes) == 1
        assert len({outcome.complaint.id for outcome in outcomes}) == 1

    assert SqliteComplaintRepository(path).count() == 10


def test_initialize_twice_keeps_the_complaints_already_stored(tmp_path: Path) -> None:
    repository = SqliteComplaintRepository(tmp_path / "reclamos.db")
    repository.initialize()
    create(repository)

    repository.initialize()

    assert repository.count() == 1


def test_initialize_creates_the_missing_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "reclamos.db"

    SqliteComplaintRepository(path).initialize()

    assert path.is_file()


def test_using_a_database_that_was_never_initialized_is_a_store_error(tmp_path: Path) -> None:
    repository = SqliteComplaintRepository(tmp_path / "reclamos.db")

    with pytest.raises(ComplaintStoreError) as refused:
        create(repository)

    assert "no such table" in refused.value.detail
    assert refused.value.public() == {
        "code": "complaint_store_error",
        "message": "The complaint could not be saved right now.",
    }


def test_a_path_that_cannot_be_opened_is_a_store_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be", encoding="utf-8")

    with pytest.raises(ComplaintStoreError, match="cannot create"):
        SqliteComplaintRepository(blocker / "reclamos.db").initialize()
    with pytest.raises(ComplaintStoreError, match="OperationalError"):
        SqliteComplaintRepository(tmp_path).count()  # a directory is not a database


def test_a_file_that_is_not_a_database_is_a_store_error(tmp_path: Path) -> None:
    path = tmp_path / "reclamos.db"
    path.write_bytes(b"this is not a sqlite database" * 100)

    with pytest.raises(ComplaintStoreError, match="DatabaseError"):
        SqliteComplaintRepository(path).initialize()


def test_a_stored_row_that_breaks_the_contract_is_a_store_error(
    repository: SqliteComplaintRepository, tmp_path: Path
) -> None:
    with closing(sqlite3.connect(tmp_path / "var" / "reclamos.db")) as raw, raw:
        raw.execute(
            "INSERT INTO complaints (idempotency_key, conversation_id, reason, description, "
            "created_at) VALUES ('bad', 'c', 'x', 'too short', '2026-10-23T15:30:00+00:00')"
        )

    with pytest.raises(ComplaintStoreError, match="stored complaint 1 is unreadable"):
        repository.find_by_key("bad")


# --- the faulty decorator over the real database ---------------------------------------


def faulty(repository: SqliteComplaintRepository, mode: FaultMode) -> FaultyComplaintRepository:
    # Seed 4 at rate 0.5: the first call faults, the second does not (see the unit tests).
    injector = FaultInjector(FaultConfig(rate=0.5, seed=4, modes=frozenset({mode})))
    return FaultyComplaintRepository(repository, injector)


def test_a_lost_acknowledgement_writes_the_row_and_the_retry_finds_it(
    repository: SqliteComplaintRepository,
) -> None:
    flaky = faulty(repository, FaultMode.LOST_ACK)
    key = idempotency_key("conv-1", DRAFT)

    with pytest.raises(SimulatedTimeout):
        flaky.create(DRAFT, conversation_id="conv-1", key=key, now=NOW)
    assert repository.count() == 1  # the caller saw a failure, yet the row exists

    retry = flaky.create(DRAFT, conversation_id="conv-1", key=key, now=NOW)

    assert (retry.created, retry.complaint.id) == (False, "REC-000001")
    assert repository.count() == 1


@pytest.mark.parametrize(
    ("mode", "failure"),
    [(FaultMode.TIMEOUT, SimulatedTimeout), (FaultMode.ERROR, SimulatedFailure)],
)
def test_a_timeout_or_an_error_leaves_no_row_and_the_retry_creates_it(
    repository: SqliteComplaintRepository, mode: FaultMode, failure: type[Exception]
) -> None:
    flaky = faulty(repository, mode)
    key = idempotency_key("conv-1", DRAFT)

    with pytest.raises(failure):
        flaky.create(DRAFT, conversation_id="conv-1", key=key, now=NOW)
    assert repository.count() == 0

    retry = flaky.create(DRAFT, conversation_id="conv-1", key=key, now=NOW)

    assert (retry.created, retry.complaint.id) == (True, "REC-000001")
