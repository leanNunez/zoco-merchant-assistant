"""The journal of runs of one company: one command, one execution (brief section 5).

A run is admitted once per `run_id`. Repeating a finished command returns its stored result
without executing anything again, and only one run may hold a conversation at a time, so two
copies of the same message cannot act in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cortex.identity import AuthContext

if TYPE_CHECKING:
    from cortex.memory.database import Database


class Admission(StrEnum):
    ADMITTED = "ADMITTED"
    REPLAY = "REPLAY"  # the same command already finished: here is its result
    BUSY = "BUSY"  # the conversation is held by another run, or this one is still running
    CONFLICT = "CONFLICT"  # the run_id was used for a different request
    # The same command was interrupted by a crash. It is not run again under the same id: it may
    # already have delivered a reply. Retrying it is a new command, with a new run_id.
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    admission: Admission
    stored_result: str | None = None


class RunJournal:
    def __init__(self, database: Database, auth: AuthContext) -> None:
        self._db = database
        self.tenant_id = auth.tenant_id

    def admit(self, run_id: str, conversation_id: str, digest: str) -> AdmissionResult:
        with self._db.transaction() as tx:
            row = tx.execute(
                "SELECT conversation_id, request_digest, status, result FROM runs "
                "WHERE tenant_id = ? AND run_id = ?",
                (self.tenant_id, run_id),
            ).fetchone()
            if row is not None:
                same_request = (row[0], row[1]) == (conversation_id, digest)
                if not same_request:
                    return AdmissionResult(Admission.CONFLICT)
                if row[2] == "FINISHED":
                    return AdmissionResult(Admission.REPLAY, row[3])
                if row[2] == "RUNNING":
                    return AdmissionResult(Admission.BUSY)
                return AdmissionResult(Admission.INTERRUPTED)
            leased = tx.execute(
                "UPDATE conversations SET active_run_id = ? "
                "WHERE tenant_id = ? AND conversation_id = ? AND active_run_id IS NULL",
                (run_id, self.tenant_id, conversation_id),
            ).rowcount
            if leased != 1:
                return AdmissionResult(Admission.BUSY)
            tx.execute(
                "INSERT INTO runs (tenant_id, run_id, conversation_id, request_digest, status, "
                "started_at) VALUES (?, ?, ?, ?, 'RUNNING', ?)",
                (self.tenant_id, run_id, conversation_id, digest, self._db.now()),
            )
        return AdmissionResult(Admission.ADMITTED)

    def finish(self, run_id: str, conversation_id: str, result: str) -> None:
        """Store the result and release the conversation, in one transaction."""
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE runs SET status = 'FINISHED', result = ? "
                "WHERE tenant_id = ? AND run_id = ?",
                (result, self.tenant_id, run_id),
            )
            tx.execute(
                "UPDATE conversations SET active_run_id = NULL "
                "WHERE tenant_id = ? AND conversation_id = ? AND active_run_id = ?",
                (self.tenant_id, conversation_id, run_id),
            )
