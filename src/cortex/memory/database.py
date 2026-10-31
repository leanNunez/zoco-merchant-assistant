"""The SQLite database of the harness (brief section 4).

One connection per unit of work, short `BEGIN IMMEDIATE` transactions that are never held open
while a model or a tool is called, WAL mode so readers do not block the writer, and foreign keys
on. The only way to read or write company data is `for_tenant`, which needs an authenticated
context.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from cortex.identity import AuthContext
from cortex.memory.tenant_store import TenantStore
from merchant_assistant.shared.clock import Clock

_BUSY_TIMEOUT_SECONDS = 5.0

# The only statements that span every company: after a restart nothing is running, whoever it
# belonged to. They read no company data. A test allows exactly these and nothing else.
RECOVERY_STATEMENTS = (
    "UPDATE conversations SET active_run_id = NULL WHERE active_run_id IS NOT NULL",
    "UPDATE runs SET status = 'FAILED' WHERE status = 'RUNNING'",
    "UPDATE operations SET status = 'UNCERTAIN' WHERE status = 'STARTED'",
)


class Database:
    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock

    def initialize(self) -> None:
        schema = files("cortex.memory").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(schema)

    def recover(self) -> None:
        """After a restart, nothing is running: free the conversations, mark the interrupted
        runs as failed, and mark every write that was in flight as uncertain, so it is
        reconciled by its id before anything else touches it. It reads no company data."""
        with self.transaction() as tx:
            for statement in RECOVERY_STATEMENTS:
                tx.execute(statement)

    def for_tenant(self, auth: AuthContext) -> TenantStore:
        return TenantStore(self, auth)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: it takes the write lock up front, so a check and the write that
        depends on it cannot be interleaved with another writer."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            yield connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: the transactions are explicit, nothing is opened implicitly.
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def now(self) -> str:
        return self.clock.now().isoformat()
