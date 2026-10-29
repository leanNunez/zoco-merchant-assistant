"""Pending writes and the idempotency ledger of one company (brief sections 3 and 5).

A pending action is a write the model proposed: it waits for the exact confirmation of its
values. The operation ledger has one row per write command, keyed by `(tenant_id,
operation_id)`, and records whether the effect is committed, failed or uncertain. Both survive a
restart because they live in SQLite, not in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cortex.identity import AuthContext

if TYPE_CHECKING:
    from cortex.memory.database import Database


class PendingStatus(StrEnum):
    PENDING = "PENDING"  # proposed, waiting for the user's confirmation
    CONFIRMED = "CONFIRMED"  # confirmed for these exact values at this control version
    SUSPENDED = "SUSPENDED"  # kept for the operator after a takeover; not executable
    CONSUMED = "CONSUMED"  # executed
    CANCELLED = "CANCELLED"  # refused or replaced


OPEN_STATUSES = (PendingStatus.PENDING, PendingStatus.CONFIRMED, PendingStatus.SUSPENDED)


class OperationStatus(StrEnum):
    STARTED = "STARTED"  # the request is leaving: if the process dies now, the outcome is unknown
    COMMITTED = "COMMITTED"
    UNCERTAIN = "UNCERTAIN"  # it may have been applied: reconcile before anything else
    FAILED = "FAILED"  # refused for sure: nothing was applied


@dataclass(frozen=True, slots=True)
class PendingAction:
    operation_id: str
    conversation_id: str
    tool: str
    arguments: str | None  # canonical JSON of the validated arguments
    fingerprint: str | None
    control_version: int
    status: PendingStatus


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    tool: str
    fingerprint: str
    status: OperationStatus
    result: str | None
    run_id: str


_PENDING_COLUMNS = (
    "operation_id, conversation_id, tool, arguments, fingerprint, control_version, status"
)


class TenantLedger:
    def __init__(self, database: Database, auth: AuthContext) -> None:
        self._db = database
        self.tenant_id = auth.tenant_id

    # ------------------------------------------------------------------ pending actions

    def record_pending(self, action: PendingAction) -> None:
        """Store a pending action, replacing whatever was pending under the same id."""
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO pending_actions (tenant_id, operation_id, conversation_id, tool, "
                "arguments, fingerprint, control_version, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (tenant_id, operation_id) DO UPDATE SET "
                "conversation_id = excluded.conversation_id, tool = excluded.tool, "
                "arguments = excluded.arguments, fingerprint = excluded.fingerprint, "
                "control_version = excluded.control_version, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (
                    self.tenant_id,
                    action.operation_id,
                    action.conversation_id,
                    action.tool,
                    action.arguments,
                    action.fingerprint,
                    action.control_version,
                    action.status,
                    self._db.now(),
                ),
            )

    def move_pending(
        self, operation_id: str, *, from_status: PendingStatus, to_status: PendingStatus
    ) -> bool:
        """Change the status only if it is still `from_status`. True when it changed."""
        with self._db.transaction() as tx:
            changed = tx.execute(
                "UPDATE pending_actions SET status = ?, updated_at = ? "
                "WHERE tenant_id = ? AND operation_id = ? AND status = ?",
                (to_status, self._db.now(), self.tenant_id, operation_id, from_status),
            ).rowcount
        return changed == 1

    def pending_action(self, operation_id: str) -> PendingAction | None:
        with self._db.reading() as db:
            row = db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_actions "  # noqa: S608 - fixed columns
                "WHERE tenant_id = ? AND operation_id = ?",
                (self.tenant_id, operation_id),
            ).fetchone()
        return None if row is None else _pending(row)

    def open_actions(self, conversation_id: str) -> tuple[PendingAction, ...]:
        """Pending, confirmed and suspended actions of a conversation, oldest first."""
        with self._db.reading() as db:
            rows = db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_actions "  # noqa: S608 - fixed columns
                "WHERE tenant_id = ? AND conversation_id = ? AND status IN (?, ?, ?) "
                "ORDER BY rowid",
                (self.tenant_id, conversation_id, *OPEN_STATUSES),
            ).fetchall()
        return tuple(_pending(row) for row in rows)

    # ------------------------------------------------------------------ operations

    def operation(self, operation_id: str) -> Operation | None:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT operation_id, tool, fingerprint, status, result, run_id FROM operations "
                "WHERE tenant_id = ? AND operation_id = ?",
                (self.tenant_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        return Operation(row[0], row[1], row[2], OperationStatus(row[3]), row[4], row[5])

    def start_operation(self, operation_id: str, tool: str, fingerprint: str, run_id: str) -> bool:
        """Claim the command before calling the outside world. False if someone else holds it.

        A command that failed for sure (nothing applied) may be started again; any other
        existing row means the command is in flight, uncertain or done.
        """
        with self._db.transaction() as tx:
            inserted = tx.execute(
                "INSERT INTO operations (tenant_id, operation_id, tool, fingerprint, status, "
                "run_id, updated_at) VALUES (?, ?, ?, ?, 'STARTED', ?, ?) "
                "ON CONFLICT (tenant_id, operation_id) DO UPDATE SET status = 'STARTED', "
                "run_id = excluded.run_id, updated_at = excluded.updated_at "
                "WHERE operations.status = 'FAILED' "
                "AND operations.fingerprint = excluded.fingerprint",
                (self.tenant_id, operation_id, tool, fingerprint, run_id, self._db.now()),
            ).rowcount
        return inserted == 1

    def settle_operation(
        self, operation_id: str, status: OperationStatus, result: str | None = None
    ) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE operations SET status = ?, result = ?, updated_at = ? "
                "WHERE tenant_id = ? AND operation_id = ?",
                (status, result, self._db.now(), self.tenant_id, operation_id),
            )


def _pending(row: tuple[str, str, str, str | None, str | None, int, str]) -> PendingAction:
    operation_id, conversation_id, tool, arguments, fingerprint, version, status = row
    return PendingAction(
        operation_id, conversation_id, tool, arguments, fingerprint, version, PendingStatus(status)
    )
