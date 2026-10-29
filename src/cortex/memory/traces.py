"""The stored traces of one company (brief section 7)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cortex.identity import AuthContext
from cortex.observability.tracer import TraceEvent

if TYPE_CHECKING:
    from cortex.memory.database import Database


class TraceStore:
    def __init__(self, database: Database, auth: AuthContext) -> None:
        self._db = database
        self.tenant_id = auth.tenant_id

    def write(self, event: TraceEvent) -> None:
        if event.tenant_id != self.tenant_id:
            raise ValueError("a trace event of another company cannot be stored here")
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO trace_events (tenant_id, run_id, seq, conversation_id, type, step, "
                "timestamp, elapsed_ms, attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                # A replayed run never rewrites the trace of the original one.
                "ON CONFLICT (tenant_id, run_id, seq) DO NOTHING",
                (
                    self.tenant_id,
                    event.run_id,
                    event.seq,
                    event.conversation_id,
                    event.type,
                    event.step,
                    event.timestamp,
                    event.elapsed_ms,
                    json.dumps(dict(event.attributes), sort_keys=True),
                ),
            )

    def events_of(self, run_id: str) -> tuple[TraceEvent, ...]:
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT seq, type, conversation_id, step, timestamp, elapsed_ms, attributes "
                "FROM trace_events WHERE tenant_id = ? AND run_id = ? ORDER BY seq",
                (self.tenant_id, run_id),
            ).fetchall()
        return tuple(
            TraceEvent(
                seq=seq,
                type=type_,
                run_id=run_id,
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                step=step,
                timestamp=timestamp,
                elapsed_ms=elapsed_ms,
                attributes=json.loads(attributes),
            )
            for seq, type_, conversation_id, step, timestamp, elapsed_ms, attributes in rows
        )
