"""Pending writes and the idempotency ledger of one company (brief sections 3 and 5).

A pending action is a write the model proposed: it waits for the exact confirmation of its
values. The operation ledger has one row per write command, keyed by `(tenant_id,
operation_id)`, and records whether the effect is committed, failed or uncertain. Both survive a
restart because they live in SQLite, not in memory.
"""

from __future__ import annotations

import json
import sqlite3
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


class StartResult(StrEnum):
    """Why a command could, or could not, be claimed for execution."""

    STARTED = "STARTED"
    CONTROL_CHANGED = "CONTROL_CHANGED"  # a person took the conversation, or its version moved
    NOT_CONFIRMED = "NOT_CONFIRMED"  # no valid confirmation of these exact values right now
    IN_PROGRESS = "IN_PROGRESS"  # someone else holds it, or it is already done


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

    def view(self) -> dict[str, object]:
        """The action with its exact values, for the model and the operator: never a summary."""
        return {
            "tool": self.tool,
            "operation_id": self.operation_id,
            "status": self.status.value,
            "arguments": json.loads(self.arguments) if self.arguments else None,
        }


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
        """Store a pending action as given. Only for state reported by a trusted system of record
        (the evaluation's preconditions); a run uses `propose`, which checks the control."""
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

    def propose(self, action: PendingAction) -> bool:
        """Record a write waiting for confirmation, only if the agent still controls the
        conversation at the action's version. False: a person took over, nothing is recorded."""
        with self._db.transaction() as tx:
            if not _in_control(tx, self.tenant_id, action.conversation_id, action.control_version):
                return False
            tx.execute(
                "INSERT INTO pending_actions (tenant_id, operation_id, conversation_id, tool, "
                "arguments, fingerprint, control_version, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?) "
                "ON CONFLICT (tenant_id, operation_id) DO UPDATE SET "
                "conversation_id = excluded.conversation_id, tool = excluded.tool, "
                "arguments = excluded.arguments, fingerprint = excluded.fingerprint, "
                "control_version = excluded.control_version, status = 'PENDING', "
                "updated_at = excluded.updated_at "
                "WHERE pending_actions.status != 'CONSUMED'",
                (
                    self.tenant_id,
                    action.operation_id,
                    action.conversation_id,
                    action.tool,
                    action.arguments,
                    action.fingerprint,
                    action.control_version,
                    self._db.now(),
                ),
            )
        return True

    def confirm_pending(
        self, operation_id: str, conversation_id: str, control_version: int
    ) -> bool:
        """The user's "sí": confirm the action asked at this very control version, and only if
        the agent still controls the conversation. The version is compared, never re-stamped."""
        with self._db.transaction() as tx:
            if not _in_control(tx, self.tenant_id, conversation_id, control_version):
                return False
            changed = tx.execute(
                "UPDATE pending_actions SET status = 'CONFIRMED', updated_at = ? "
                "WHERE tenant_id = ? AND operation_id = ? AND conversation_id = ? "
                "AND status = 'PENDING' AND control_version = ?",
                (self._db.now(), self.tenant_id, operation_id, conversation_id, control_version),
            ).rowcount
        return changed == 1

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

    def start_operation(
        self,
        operation_id: str,
        tool: str,
        fingerprint: str,
        run_id: str,
        *,
        conversation_id: str,
        control_version: int,
    ) -> StartResult:
        """Claim the command right before calling the outside world.

        One write transaction checks everything that must still be true, so nothing can change
        between the check and the claim: the agent controls the conversation at that version, the
        exact values are confirmed there, and nobody else holds the command. A command that
        failed for sure, or whose uncertain outcome was looked up and found not applied, may be
        claimed again; a running or committed one may not.
        """
        with self._db.transaction() as tx:
            if not _in_control(tx, self.tenant_id, conversation_id, control_version):
                return StartResult.CONTROL_CHANGED
            confirmed = tx.execute(
                "SELECT 1 FROM pending_actions WHERE tenant_id = ? AND operation_id = ? "
                "AND conversation_id = ? AND status = 'CONFIRMED' AND fingerprint = ? "
                "AND control_version = ?",
                (self.tenant_id, operation_id, conversation_id, fingerprint, control_version),
            ).fetchone()
            if confirmed is None:
                return StartResult.NOT_CONFIRMED
            claimed = tx.execute(
                "INSERT INTO operations (tenant_id, operation_id, tool, fingerprint, status, "
                "run_id, updated_at) VALUES (?, ?, ?, ?, 'STARTED', ?, ?) "
                "ON CONFLICT (tenant_id, operation_id) DO UPDATE SET status = 'STARTED', "
                "run_id = excluded.run_id, updated_at = excluded.updated_at "
                "WHERE operations.status IN ('FAILED', 'UNCERTAIN') "
                "AND operations.fingerprint = excluded.fingerprint",
                (self.tenant_id, operation_id, tool, fingerprint, run_id, self._db.now()),
            ).rowcount
        return StartResult.STARTED if claimed == 1 else StartResult.IN_PROGRESS

    def owner_has_finished(self, operation_id: str) -> bool:
        """True when a STARTED command belongs to a run that is known to be over: its owner can
        no longer settle it, so it is as uncertain as a crash. An unknown owner counts as alive."""
        with self._db.reading() as db:
            row = db.execute(
                "SELECT r.status FROM operations o JOIN runs r "
                "ON r.tenant_id = o.tenant_id AND r.run_id = o.run_id "
                "WHERE o.tenant_id = ? AND o.operation_id = ? AND o.status = 'STARTED'",
                (self.tenant_id, operation_id),
            ).fetchone()
        return row is not None and row[0] != "RUNNING"

    def abandon_started(self, operation_id: str) -> None:
        """A STARTED command nobody will settle becomes UNCERTAIN, to be looked up by its id."""
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE operations SET status = 'UNCERTAIN', updated_at = ? "
                "WHERE tenant_id = ? AND operation_id = ? AND status = 'STARTED'",
                (self._db.now(), self.tenant_id, operation_id),
            )

    def settle_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        result: str | None = None,
        *,
        effect_in: str | None = None,
    ) -> None:
        """Record the outcome. `effect_in` names the conversation where a committed effect is
        journaled, in the same transaction."""
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE operations SET status = ?, result = ?, updated_at = ? "
                "WHERE tenant_id = ? AND operation_id = ?",
                (status, result, self._db.now(), self.tenant_id, operation_id),
            )
            if effect_in is not None and status is OperationStatus.COMMITTED:
                tx.execute(
                    "INSERT INTO journal (tenant_id, conversation_id, kind, reference, "
                    "created_at) VALUES (?, ?, 'effect', ?, ?)",
                    (self.tenant_id, effect_in, operation_id, self._db.now()),
                )
                # Executed: whatever was still open about it (even suspended) is now done.
                tx.execute(
                    "UPDATE pending_actions SET status = 'CONSUMED', updated_at = ? "
                    "WHERE tenant_id = ? AND operation_id = ? "
                    "AND status IN ('PENDING', 'CONFIRMED', 'SUSPENDED')",
                    (self._db.now(), self.tenant_id, operation_id),
                )

    def uncertain_operations(self, conversation_id: str) -> tuple[str, ...]:
        """The commands of this conversation whose outcome is still unknown."""
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT o.operation_id FROM operations o JOIN pending_actions p "
                "ON p.tenant_id = o.tenant_id AND p.operation_id = o.operation_id "
                "WHERE o.tenant_id = ? AND p.conversation_id = ? "
                "AND o.status IN ('UNCERTAIN', 'STARTED') ORDER BY o.operation_id",
                (self.tenant_id, conversation_id),
            ).fetchall()
        return tuple(row[0] for row in rows)


def _pending(row: tuple[str, str, str, str | None, str | None, int, str]) -> PendingAction:
    operation_id, conversation_id, tool, arguments, fingerprint, version, status = row
    return PendingAction(
        operation_id, conversation_id, tool, arguments, fingerprint, version, PendingStatus(status)
    )


def _in_control(
    tx: sqlite3.Connection, tenant_id: str, conversation_id: str, control_version: int
) -> bool:
    """The agent still controls the conversation, at exactly this version."""
    row = tx.execute(
        "SELECT mode, control_version FROM conversations "
        "WHERE tenant_id = ? AND conversation_id = ?",
        (tenant_id, conversation_id),
    ).fetchone()
    return row is not None and row[0] == "AUTO" and row[1] == control_version
