"""How a write runs (brief sections 3 and 5): confirmed, once, and never retried blindly.

1. The ledger is checked first, by `(company, operation_id)`. A committed command with the same
   values is answered from the ledger without calling the tool again; the same id with other
   values is refused. A command whose outcome is uncertain is only looked up: if it was applied,
   it is recorded; if not, it starts over and needs a valid confirmation like any first attempt.
2. The exact values must be confirmed at the current control version. Otherwise the action is
   proposed (only while the agent still controls the conversation) and the user is asked.
3. The command is claimed in the ledger (STARTED) in one transaction that also checks the
   control and the confirmation, so a takeover or a cancellation cannot slip in between the
   check and the call, and two runs cannot execute it at the same time.
4. Any failure that is not a certain refusal makes the outcome uncertain: the command is looked
   up by its id and, if it was not applied, claimed and attempted once more with the same key,
   which the claims store's UNIQUE constraint makes harmless.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from cortex.memory.ledger import (
    OperationStatus,
    PendingAction,
    PendingStatus,
    StartResult,
    TenantLedger,
)
from cortex.tools.confirmation import (
    canonical_arguments,
    confirmation_question,
    fingerprint,
    is_confirmed,
)
from cortex.tools.failures import FailureKind, ToolFailure
from cortex.tools.gateway import ToolResult, WriteAdapter
from cortex.tools.policy import Approved
from merchant_assistant.shared.errors import DomainError


def _ignore(type_: str, **attributes: str | int | bool | None) -> None:
    """The default when nobody listens to the write's own events."""


@dataclass(frozen=True, slots=True)
class WriteContext:
    ledger: TenantLedger
    conversation_id: str
    control_version: int
    run_id: str
    # Where the write reports what it tries: a failed attempt, a lookup by operation id.
    trace: Callable[..., None] = _ignore


@dataclass(frozen=True, slots=True)
class Done:
    result: ToolResult
    how: Literal["executed", "deduplicated", "reconciled"]


@dataclass(frozen=True, slots=True)
class NeedsConfirmation:
    operation_id: str
    question: str


@dataclass(frozen=True, slots=True)
class Refused:
    code: Literal["operation_id_conflict", "operation_in_progress", "confirmation_missing"]
    detail: str


@dataclass(frozen=True, slots=True)
class ControlChanged:
    """A person took the conversation (or its version moved) before the write could run."""

    operation_id: str


@dataclass(frozen=True, slots=True)
class Failed:
    result: ToolResult


@dataclass(frozen=True, slots=True)
class Unresolved:
    operation_id: str


WriteOutcome = Done | NeedsConfirmation | Refused | Failed | Unresolved | ControlChanged


def write(approved: Approved, adapter: WriteAdapter, context: WriteContext) -> WriteOutcome:
    name, arguments = approved.spec.name, approved.arguments
    operation_id = _operation_id(arguments)
    values = fingerprint(name, arguments)
    ledger = context.ledger

    existing = ledger.operation(operation_id)
    if existing is not None:
        if existing.fingerprint != values:
            return Refused("operation_id_conflict", "the id was already used with other values")
        if existing.status is OperationStatus.COMMITTED:
            stored = json.loads(existing.result) if existing.result else {}
            return Done(_ok(name, adapter, stored), "deduplicated")
        if existing.status is OperationStatus.STARTED and ledger.owner_has_finished(operation_id):
            # Its run ended without settling it (a crash in the middle): its outcome is unknown.
            ledger.abandon_started(operation_id)
            existing = ledger.operation(operation_id)
        if existing is not None and existing.status is OperationStatus.STARTED:
            return Refused("operation_in_progress", "another run is executing this command")
        if existing is not None and existing.status is OperationStatus.UNCERTAIN:
            found = _look_up(approved, adapter, context, operation_id)
            if found is not None:
                return found
            # It was not applied. Trying it again is a new attempt: it needs a valid confirmation.

    pending = ledger.pending_action(operation_id)
    if pending is not None and pending.conversation_id != context.conversation_id:
        return Refused("operation_id_conflict", "the id belongs to another conversation")
    if not is_confirmed(pending, values, context.control_version):
        proposed = ledger.propose(
            PendingAction(
                operation_id=operation_id,
                conversation_id=context.conversation_id,
                tool=name,
                arguments=canonical_arguments(arguments),
                fingerprint=values,
                control_version=context.control_version,
                status=PendingStatus.PENDING,
            )
        )
        if not proposed:
            return ControlChanged(operation_id)
        return NeedsConfirmation(operation_id, confirmation_question(name, arguments))
    return _execute(approved, adapter, context, operation_id, values, may_retry=True)


def _execute(
    approved: Approved,
    adapter: WriteAdapter,
    context: WriteContext,
    operation_id: str,
    values: str,
    *,
    may_retry: bool,
) -> WriteOutcome:
    """Claim the command (checking control and confirmation in the same transaction), call the
    outside world, and settle what happened."""
    name = approved.spec.name
    started = context.ledger.start_operation(
        operation_id,
        name,
        values,
        context.run_id,
        conversation_id=context.conversation_id,
        control_version=context.control_version,
    )
    if started is StartResult.CONTROL_CHANGED:
        return ControlChanged(operation_id)
    if started is StartResult.NOT_CONFIRMED:
        return Refused(
            "confirmation_missing", "the confirmation of these values is no longer valid"
        )
    if started is StartResult.IN_PROGRESS:
        return Refused("operation_in_progress", "another run is executing this command")
    try:
        data = adapter(approved.arguments, conversation_id=context.conversation_id)
    except DomainError as failure:
        # Only a certain refusal proves nothing was applied; a timeout or a lost answer does not.
        uncertain = isinstance(failure, ToolFailure) and failure.kind is not FailureKind.PERMANENT
        if not uncertain:
            context.ledger.settle_operation(operation_id, OperationStatus.FAILED)
            context.ledger.move_pending(
                operation_id, from_status=PendingStatus.CONFIRMED, to_status=PendingStatus.CANCELLED
            )
            return Failed(
                ToolResult(name, ok=False, source_tenant=adapter.tenant_id, error=failure.public())
            )
        context.ledger.settle_operation(operation_id, OperationStatus.UNCERTAIN)
        context.trace("write_attempt_failed", tool=name, reason=failure.code)
        if not may_retry:
            return Unresolved(operation_id)
        found = _look_up(approved, adapter, context, operation_id)
        if found is not None:
            return found
        # Not applied: one more attempt with the same key, claimed and checked again. If the
        # first attempt commits late, the store's UNIQUE key turns this one into a read of it.
        retried = _execute(approved, adapter, context, operation_id, values, may_retry=False)
        return Done(retried.result, "reconciled") if isinstance(retried, Done) else retried
    except Exception:
        # Anything unexpected after the claim: the effect may have happened. It must not stay
        # STARTED (that would block the command); UNCERTAIN gets it looked up next time.
        context.ledger.settle_operation(operation_id, OperationStatus.UNCERTAIN)
        raise
    return _commit(name, adapter, context, operation_id, data, "executed")


def _look_up(
    approved: Approved, adapter: WriteAdapter, context: WriteContext, operation_id: str
) -> WriteOutcome | None:
    """Was the command applied? `Done` if it was, `Unresolved` if nobody can tell, `None` if not.
    A lookup never applies anything, so it needs no confirmation."""
    name = approved.spec.name
    try:
        found = adapter.lookup(operation_id)
    except DomainError:
        context.trace("write_lookup", tool=name, found=None)  # it could not even be checked
        return Unresolved(operation_id)
    context.trace("write_lookup", tool=name, found=found is not None)
    if found is None:
        return None
    return _commit(name, adapter, context, operation_id, found, "reconciled")


def _commit(
    name: str,
    adapter: WriteAdapter,
    context: WriteContext,
    operation_id: str,
    data: Mapping[str, object],
    how: Literal["executed", "reconciled"],
) -> Done:
    # Recorded with the effect, and the pending action (even a suspended one) is consumed.
    context.ledger.settle_operation(
        operation_id,
        OperationStatus.COMMITTED,
        json.dumps(dict(data), ensure_ascii=False),
        effect_in=context.conversation_id,
    )
    return Done(_ok(name, adapter, data), how)


def _ok(name: str, adapter: WriteAdapter, data: Mapping[str, object]) -> ToolResult:
    return ToolResult(name, ok=True, source_tenant=adapter.tenant_id, data=data)


def _operation_id(arguments: object) -> str:
    operation_id = getattr(arguments, "operation_id", None)
    if not isinstance(operation_id, str):
        raise TypeError("a write tool's arguments must carry an operation_id")
    return operation_id
