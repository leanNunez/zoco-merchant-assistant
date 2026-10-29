"""A read-only view of the whole database, used only to measure a scenario's outcome.

It crosses companies on purpose: it is the evaluator's view, and it exists to prove that the
harness never does. It opens the file read-only and lives in the kit adapter, never in the
harness itself.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


class Audit:
    def __init__(self, database_path: Path) -> None:
        self._uri = f"{database_path.resolve().as_uri()}?mode=ro"

    def rows_written_outside(self, tenant_id: str, run_id: str) -> int:
        """Rows this run wrote under any company other than its own."""
        total = 0
        for table in ("messages", "outbox"):
            total += self._count(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ? AND tenant_id != ?",  # noqa: S608 - fixed table names
                (run_id, tenant_id),
            )
        return total

    def messages(self, tenant_id: str, conversation_id: str) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT content FROM messages WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchall()
        return {content for (content,) in rows}

    def _count(self, sql: str, params: tuple[str, ...]) -> int:
        with self._connect() as db:
            return int(db.execute(sql, params).fetchone()[0])

    def _connect(self) -> closing[sqlite3.Connection]:
        # `closing`: a sqlite3 connection used as a context manager commits, it does not close.
        return closing(sqlite3.connect(self._uri, uri=True))
