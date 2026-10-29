"""What Zoco Chat needs to remember about a conversation (brief section 6), for one company.

The control of the conversation changes here when an operator takes over: one write
transaction checks the version, hands the conversation to the person, and suspends every pending
write, so none of them can run while somebody else is in control.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cortex.identity import AuthContext
from merchant_assistant.control.states import State

if TYPE_CHECKING:
    from cortex.memory.database import Database

JournalKind = Literal["takeover", "delivery", "effect"]


@dataclass(frozen=True, slots=True)
class Commitment:
    kind: Literal["internal_task", "contact_date"]
    responsible: str | None
    due_date: str | None


class ChatRecords:
    def __init__(self, database: Database, auth: AuthContext) -> None:
        self._db = database
        self.tenant_id = auth.tenant_id

    # ------------------------------------------------------------------ control

    def set_control(self, conversation_id: str, mode: State, version: int) -> None:
        """Set who controls a conversation, as reported by a trusted system of record."""
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE conversations SET mode = ?, control_version = ? "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (mode, version, self.tenant_id, conversation_id),
            )

    def apply_takeover(self, conversation_id: str, version: int, responsible: str) -> bool:
        """Hand the conversation to a person. False when the event is not newer than the state.

        A blocked conversation stays blocked (only a supervisor unblocks it), but it still
        records the newer version and who is now responsible.
        """
        with self._db.transaction() as tx:
            row = tx.execute(
                "SELECT mode, control_version FROM conversations "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (self.tenant_id, conversation_id),
            ).fetchone()
            if row is None or version <= row[1]:
                return False
            mode = State.BLOCKED if row[0] == State.BLOCKED else State.HUMAN
            tx.execute(
                "UPDATE conversations SET mode = ?, control_version = ?, responsible = ? "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (mode, version, responsible, self.tenant_id, conversation_id),
            )
            tx.execute(
                "UPDATE pending_actions SET status = 'SUSPENDED', updated_at = ? "
                "WHERE tenant_id = ? AND conversation_id = ? "
                "AND status IN ('PENDING', 'CONFIRMED')",
                (self._db.now(), self.tenant_id, conversation_id),
            )
            self._journal(tx, conversation_id, "takeover", f"v{version}")
        return True

    # ------------------------------------------------------------------ journal

    def last_takeover_seq(self, conversation_id: str) -> int | None:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT MAX(seq) FROM journal "
                "WHERE tenant_id = ? AND conversation_id = ? AND kind = 'takeover'",
                (self.tenant_id, conversation_id),
            ).fetchone()
        return None if row[0] is None else int(row[0])

    def count_after(self, conversation_id: str, kind: JournalKind, after_seq: int) -> int:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM journal "
                "WHERE tenant_id = ? AND conversation_id = ? AND kind = ? AND seq > ?",
                (self.tenant_id, conversation_id, kind, after_seq),
            ).fetchone()
        return int(row[0])

    def record(self, conversation_id: str, kind: JournalKind, reference: str) -> None:
        with self._db.transaction() as tx:
            self._journal(tx, conversation_id, kind, reference)

    def _journal(
        self, tx: sqlite3.Connection, conversation_id: str, kind: JournalKind, reference: str
    ) -> None:
        tx.execute(
            "INSERT INTO journal (tenant_id, conversation_id, kind, reference, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.tenant_id, conversation_id, kind, reference, self._db.now()),
        )

    # ------------------------------------------------------------------ follow-ups

    def record_followup(
        self,
        conversation_id: str,
        run_id: str,
        decision: str,
        reasons: tuple[str, ...],
        commitment: Commitment | None,
    ) -> None:
        """Store the decision and keep the commitments in line with it, in one transaction."""
        now = self._db.now()
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO followup_decisions (tenant_id, conversation_id, run_id, decision, "
                "reasons, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.tenant_id, conversation_id, run_id, decision, ",".join(reasons), now),
            )
            # A new decision replaces whatever was owed before.
            tx.execute(
                "UPDATE commitments SET status = 'CANCELLED' "
                "WHERE tenant_id = ? AND conversation_id = ? AND status = 'OPEN'",
                (self.tenant_id, conversation_id),
            )
            if commitment is not None:
                tx.execute(
                    "INSERT INTO commitments (tenant_id, conversation_id, kind, responsible, "
                    "due_date, status, created_at) VALUES (?, ?, ?, ?, ?, 'OPEN', ?)",
                    (
                        self.tenant_id,
                        conversation_id,
                        commitment.kind,
                        commitment.responsible,
                        commitment.due_date,
                        now,
                    ),
                )

    def open_commitments(self, conversation_id: str) -> tuple[Commitment, ...]:
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT kind, responsible, due_date FROM commitments "
                "WHERE tenant_id = ? AND conversation_id = ? AND status = 'OPEN' "
                "ORDER BY commitment_id",
                (self.tenant_id, conversation_id),
            ).fetchall()
        return tuple(Commitment(kind, responsible, due) for kind, responsible, due in rows)
