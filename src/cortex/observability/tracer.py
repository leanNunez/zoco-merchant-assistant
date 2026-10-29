"""Structured trace events of one run (brief section 7).

Every event carries the run, company and conversation ids, the step, the time and the elapsed
milliseconds. Attributes are filtered by the redactor at the moment they are written: only the
allowlisted ones survive, scrubbed (see `redaction.py`).

Writing a trace never changes what a run does: a sink that fails is logged once and dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC

from cortex.observability.redaction import Scalar, TraceRedactor
from merchant_assistant.shared.clock import Clock

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    seq: int
    type: str
    run_id: str
    tenant_id: str
    conversation_id: str
    step: int
    timestamp: str
    elapsed_ms: int
    attributes: Mapping[str, Scalar] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Scalar]) -> TraceEvent:
        fields = {name: data[name] for name in _BASE_FIELDS}
        attributes = {name: value for name, value in data.items() if name not in _BASE_FIELDS}
        return cls(**fields, attributes=attributes)  # type: ignore[arg-type]

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "type": self.type,
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "step": self.step,
            "timestamp": self.timestamp,
            "elapsed_ms": self.elapsed_ms,
            **self.attributes,
        }


_BASE_FIELDS = (
    "seq",
    "type",
    "run_id",
    "tenant_id",
    "conversation_id",
    "step",
    "timestamp",
    "elapsed_ms",
)


EventSink = Callable[[TraceEvent], None]


class Tracer:
    def __init__(
        self,
        *,
        run_id: str,
        tenant_id: str,
        conversation_id: str,
        clock: Clock,
        elapsed_ms: Callable[[], int],
        redactor: TraceRedactor,
    ) -> None:
        self._ids = (run_id, tenant_id, conversation_id)
        self._clock = clock
        self._elapsed_ms = elapsed_ms
        self._redactor = redactor
        self._events: list[TraceEvent] = []
        self._sink: EventSink | None = None

    def principal(self, principal_id: str) -> str:
        return self._redactor.principal(principal_id)

    def attach(self, sink: EventSink) -> None:
        """Start writing through to `sink`, beginning with what was traced so far."""
        self._sink = sink
        for event in self._events:
            self._write(event)

    def emit(self, type_: str, *, step: int, **attributes: Scalar) -> None:
        run_id, tenant_id, conversation_id = self._ids
        clean, dropped = self._redactor.clean(type_, attributes)
        if dropped:
            clean["dropped_attributes"] = dropped
        timestamp = self._clock.now().astimezone(UTC).isoformat(timespec="seconds")
        event = TraceEvent(
            seq=len(self._events) + 1,
            type=type_,
            run_id=run_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            step=step,
            timestamp=timestamp.replace("+00:00", "Z"),
            elapsed_ms=self._elapsed_ms(),
            attributes=clean,
        )
        self._events.append(event)
        self._write(event)

    def count(self, type_: str) -> int:
        return sum(1 for event in self._events if event.type == type_)

    def _write(self, event: TraceEvent) -> None:
        if self._sink is None:
            return
        try:
            self._sink(event)
        except Exception:
            # Observability is never on the critical path: log once, stop writing, go on.
            _LOG.warning("trace sink failed for run %s; tracing continues in memory", event.run_id)
            self._sink = None

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)
