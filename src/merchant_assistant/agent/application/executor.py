"""Running tools: lookups when the guards allow them, the one effect only on a `Confirmed`.

Two entry points, on purpose:

- `run_read` runs a READ tool the guards already authorized.
- `execute_confirmed` is the ONLY way the agent can register a complaint, and it accepts
  nothing but a `Confirmed`, which only the confirmation gate can build. An architecture test
  fails if any other module of the agent calls `ComplaintRegistry.register`.

Failures are retried only when the error says it is worth it (`retryable`, the simulated
`ToolFailure`s), with exponential backoff and jitter through an injected sleeper and a seeded
random generator, so tests never wait and a run can be replayed. A refusal (an invalid or
future date, an invalid complaint) is never retried: the same input would be refused again.

What the model gets back is `envelope`: one JSON object per tool message, marked as untrusted
data, with document text only inside JSON string values. Exception text, tracebacks and the
`detail` of an error never enter it; only stable codes and the errors' fixed safe messages do.

A port that raises anything that is not a `DomainError` (a bug, an unexpected driver error) is
logged here with its traceback, for the operators of the system, and becomes a non-retryable
`internal_error` with a fixed message: the same input would most probably fail again, and the
customer must never see the exception's text.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeVar

from merchant_assistant.agent.domain.guards import ValidatedCall
from merchant_assistant.agent.domain.pending import Confirmed
from merchant_assistant.agent.domain.tools import (
    BUSCAR_DOCUMENTACION,
    CONSULTAR_VENTAS,
    BuscarDocumentacionArgs,
    ConsultarVentasArgs,
    CrearReclamoArgs,
    ToolKind,
)
from merchant_assistant.agent.ports import (
    ComplaintRegistry,
    KnowledgeSearch,
    Registration,
    SalesLookup,
    SalesOutcome,
    SearchOutcome,
)
from merchant_assistant.shared.errors import DomainError

_log = logging.getLogger(__name__)

ToolStatus = Literal["ok", "no_data", "error"]
Sleeper = Callable[[float], None]
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    status: ToolStatus
    payload: dict[str, object] | None
    error_code: str | None
    retryable: bool
    attempts: int
    # The safe, fixed message of the error class, for the model. Never exception text.
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class Execution:
    """A complaint that is known to be stored.

    `created_effective` is True when this call created it, OR when an earlier attempt of this
    same call failed with a retryable error before a later one found it already stored: a lost
    acknowledgement means our own earlier attempt most probably wrote the row, so telling the
    user "it was already registered" would be misleading. (A row written by an earlier TURN is
    reported as already registered; that is the honest reading, because this call did not
    write it.)
    """

    complaint_id: str
    created_effective: bool
    attempts: int


class ExecutionFailed(DomainError):  # noqa: N818 - named after the situation
    """No attempt of the effect could be confirmed. Whether it was stored is unknown.

    Carries the code of the last error (`cause_code`), whether it was retryable, and how many
    attempts were made; the last error itself is the `__cause__`.
    """

    code: ClassVar[str] = "execution_failed"
    user_message: ClassVar[str] = "The action could not be confirmed."

    def __init__(self, cause_code: str, *, retryable: bool, attempts: int) -> None:
        super().__init__(f"cause={cause_code} retryable={retryable} attempts={attempts}")
        self.cause_code = cause_code
        self.retryable = retryable
        self.attempts = attempts


class InternalToolError(DomainError):
    """A port failed in a way no capability declared. The real exception is in the logs only."""

    code: ClassVar[str] = "internal_error"
    user_message: ClassVar[str] = "The tool failed unexpectedly. Do not retry it now."


class ToolExecutor:
    def __init__(
        self,
        knowledge: KnowledgeSearch,
        sales: SalesLookup,
        complaints: ComplaintRegistry,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.2,
        sleeper: Sleeper,
        rng: random.Random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not base_delay >= 0:
            raise ValueError("base_delay must be >= 0")
        self._knowledge = knowledge
        self._sales = sales
        self._complaints = complaints
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleeper
        self._rng = rng

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def run_read(self, call: ValidatedCall) -> ToolOutcome:
        if call.spec.kind is not ToolKind.READ:
            raise TypeError("run_read runs READ tools only; effects need a Confirmed")
        arguments = call.arguments
        if isinstance(arguments, BuscarDocumentacionArgs):
            query = arguments.consulta
            return self._read(lambda: self._knowledge.search(query), _search_payload)
        if isinstance(arguments, ConsultarVentasArgs):
            raw_date = arguments.fecha
            return self._read(lambda: self._sales.lookup(raw_date), _sales_payload)
        raise TypeError(f"no reader for {call.spec.name}")

    def execute_confirmed(self, confirmed: Confirmed, conversation_id: str) -> Execution:
        """Register the confirmed complaint. The ONLY effect entry point of the agent.

        Every attempt sends the same normalized parameters for the same conversation, so the
        capability derives the same idempotency key: a retry after a lost acknowledgement finds
        the stored complaint instead of writing a second one.
        """
        if not isinstance(confirmed, Confirmed):
            raise TypeError("an effect runs only on a Confirmed issued by the gate")
        arguments = confirmed.action.arguments
        if not isinstance(arguments, CrearReclamoArgs):
            raise TypeError(f"no executor for {confirmed.action.tool}")
        reason, description = arguments.motivo, arguments.descripcion

        def register() -> Registration:
            return self._complaints.register(reason, description, conversation_id)

        registration, attempts = self._with_retries(register)
        return Execution(
            complaint_id=registration.complaint_id,
            created_effective=registration.created or attempts > 1,
            attempts=attempts,
        )

    def _read(
        self,
        operation: Callable[[], _T],
        to_payload: Callable[[_T], tuple[ToolStatus, dict[str, object]]],
    ) -> ToolOutcome:
        try:
            result, attempts = self._with_retries(operation)
        except ExecutionFailed as failed:
            cause = failed.__cause__
            message = cause.user_message if isinstance(cause, DomainError) else None
            return ToolOutcome(
                status="error",
                payload=None,
                error_code=failed.cause_code,
                retryable=failed.retryable,
                attempts=failed.attempts,
                error_message=message,
            )
        status, payload = to_payload(result)
        return ToolOutcome(status, payload, None, retryable=False, attempts=attempts)

    def _with_retries(self, operation: Callable[[], _T]) -> tuple[_T, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return operation(), attempt
            except DomainError as error:
                retryable = _is_retryable(error)
                if not retryable or attempt >= self._max_attempts:
                    raise ExecutionFailed(
                        error.code, retryable=retryable, attempts=attempt
                    ) from error
                self._sleep(self._backoff(attempt))
            except Exception:
                _log.exception("a tool port raised an undeclared error (attempt %d)", attempt)
                internal = InternalToolError()
                raise ExecutionFailed(
                    internal.code, retryable=False, attempts=attempt
                ) from internal

    def _backoff(self, attempt: int) -> float:
        # Exponential with "equal jitter": half the step is fixed, half is random, so retries
        # of different conversations spread out but never fire immediately.
        step = self._base_delay * 2.0 ** (attempt - 1)
        return step / 2 + self._rng.uniform(0, step / 2)


def _is_retryable(error: DomainError) -> bool:
    return getattr(error, "retryable", False) is True


def _search_payload(outcome: SearchOutcome) -> tuple[ToolStatus, dict[str, object]]:
    passages = [
        {
            "doc_id": passage.doc_id,
            "valid_from": passage.valid_from.isoformat(),
            "valid_until": passage.valid_until.isoformat(),
            "text": passage.text,
        }
        for passage in outcome.passages
    ]
    return outcome.status, {"passages": passages}


def _sales_payload(outcome: SalesOutcome) -> tuple[ToolStatus, dict[str, object]]:
    figures = outcome.figures
    if figures is None:
        return "no_data", {"fecha": outcome.day.isoformat(), "registered": False}
    return "ok", {
        "fecha": outcome.day.isoformat(),
        "registered": True,
        "entradas": figures.tickets,
        "bruto": str(figures.gross),
        "devoluciones": str(figures.refunds),
        "neto": str(figures.net),
    }


EnvelopeStatus = Literal["ok", "no_data", "error", "pending_confirmation"]


def envelope(
    tool: str,
    status: EnvelopeStatus,
    *,
    data: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> str:
    """The one JSON object a tool message carries. Everything in it is data, not instructions."""
    return json.dumps(
        {"untrusted_data": True, "tool": tool, "status": status, "data": data, "error": error},
        ensure_ascii=False,
        sort_keys=False,
    )


def outcome_envelope(tool: str, outcome: ToolOutcome) -> str:
    if outcome.status == "error":
        error: dict[str, object] = {
            "code": outcome.error_code,
            "retryable": outcome.retryable,
            "attempts": outcome.attempts,
        }
        if outcome.error_message is not None:
            error["message"] = outcome.error_message
        return envelope(tool, "error", error=error)
    return envelope(tool, outcome.status, data=outcome.payload)


def refusal_envelope(tool: str, refusal: DomainError) -> str:
    """A call the guards refused: the error's public view, so the model can correct itself."""
    error: dict[str, object] = {**refusal.public(), "retryable": False}
    return envelope(tool, "error", error=error)


READ_TOOLS = (BUSCAR_DOCUMENTACION, CONSULTAR_VENTAS)
