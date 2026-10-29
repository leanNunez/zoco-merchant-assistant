"""Structured trace events of one run (brief section 7).

Every event carries the run, company and conversation ids, the step, the time and the elapsed
milliseconds. Attributes are scalars chosen by the code (names, codes, counts), never free text
written by the user or the model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC

from merchant_assistant.shared.clock import Clock

Scalar = str | int | bool | None


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


class Tracer:
    def __init__(
        self,
        *,
        run_id: str,
        tenant_id: str,
        conversation_id: str,
        clock: Clock,
        elapsed_ms: Callable[[], int],
    ) -> None:
        self._ids = (run_id, tenant_id, conversation_id)
        self._clock = clock
        self._elapsed_ms = elapsed_ms
        self._events: list[TraceEvent] = []

    def emit(self, type_: str, *, step: int, **attributes: Scalar) -> None:
        run_id, tenant_id, conversation_id = self._ids
        timestamp = self._clock.now().astimezone(UTC).isoformat(timespec="seconds")
        self._events.append(
            TraceEvent(
                seq=len(self._events) + 1,
                type=type_,
                run_id=run_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                step=step,
                timestamp=timestamp.replace("+00:00", "Z"),
                elapsed_ms=self._elapsed_ms(),
                attributes=dict(attributes),
            )
        )

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)
