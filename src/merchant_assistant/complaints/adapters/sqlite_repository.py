"""Complaints in one SQLite file, with idempotency enforced by the database.

The guarantee that a complaint is never registered twice is the UNIQUE constraint on
`idempotency_key`, not a Python `if`. A check-then-insert has a race window: two requests
can both see "not there" and both insert. The constraint cannot be raced, whatever the
application does, so `create` inserts and lets the database decide.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from merchant_assistant.complaints.errors import ComplaintStoreError, InvalidComplaint
from merchant_assistant.complaints.models import Complaint, ComplaintDraft, CreateOutcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaints (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_SELECT_BY_KEY = (
    "SELECT seq, idempotency_key, conversation_id, reason, description, created_at "
    "FROM complaints WHERE idempotency_key = ?"
)


class SqliteComplaintRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def initialize(self) -> None:
        """Create the directory and the schema. Safe to call any number of times."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ComplaintStoreError(f"cannot create {self._path.parent}: {error}") from error
        with self._transaction() as connection:
            connection.execute(SCHEMA)

    def create(
        self, draft: ComplaintDraft, *, conversation_id: str, key: str, now: datetime
    ) -> CreateOutcome:
        if now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        stored_at = now.astimezone(UTC).isoformat()
        with self._transaction() as connection:
            # ON CONFLICT DO NOTHING + RETURNING: a new row returns its seq, a duplicate
            # key returns nothing. The insert and the follow-up read share one transaction.
            inserted = connection.execute(
                "INSERT INTO complaints "
                "(idempotency_key, conversation_id, reason, description, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING RETURNING seq",
                (key, conversation_id, draft.reason, draft.description, stored_at),
            ).fetchall()
            row = connection.execute(_SELECT_BY_KEY, (key,)).fetchone()
            if not inserted:
                # AUTOINCREMENT advances its counter even when DO NOTHING skips the row.
                # Rolling back leaves the counter alone, so a retry does not leave a gap in
                # the public ids (REC-000001, REC-000002, ...).
                connection.rollback()
        if row is None:  # unreachable unless the database lost the row it just wrote
            raise ComplaintStoreError(f"complaint under key {key} vanished after the insert")
        return CreateOutcome(complaint=_complaint(row), created=bool(inserted))

    def find_by_key(self, key: str) -> Complaint | None:
        with self._transaction() as connection:
            row = connection.execute(_SELECT_BY_KEY, (key,)).fetchone()
        return None if row is None else _complaint(row)

    def count(self) -> int:
        with self._transaction() as connection:
            row = connection.execute("SELECT COUNT(*) FROM complaints").fetchone()
        return int(row[0])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # A new connection per operation: threads never share one. `with connection`
        # commits or rolls back but does not close; `closing` does.
        try:
            with closing(sqlite3.connect(self._path)) as connection, connection:
                yield connection
        except sqlite3.Error as error:
            raise ComplaintStoreError(f"{type(error).__name__}: {error}") from error


def _complaint(row: tuple[int, str, str, str, str, str]) -> Complaint:
    seq, key, conversation_id, reason, description, created_at = row
    try:
        return Complaint(
            id=f"REC-{seq:06d}",
            draft=ComplaintDraft(reason, description),
            conversation_id=conversation_id,
            key=key,
            created_at=datetime.fromisoformat(created_at),
        )
    except (InvalidComplaint, ValueError) as error:
        raise ComplaintStoreError(f"stored complaint {seq} is unreadable: {error}") from error
