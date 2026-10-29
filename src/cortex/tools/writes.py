"""How a write runs (brief sections 3 and 5): confirmed, once, and never retried blindly.

1. The ledger is checked first, by `(company, operation_id)`. A committed command with the same
   values is answered from the ledger without calling the tool again; the same id with other
   values is refused; an uncertain command is reconciled before anything else.
2. The exact values must have been confirmed at the current control version. Otherwise the
   action is stored as pending and the user is asked.
3. The command is claimed in the ledger (STARTED) before the outside world is called, so two
   runs cannot execute it at the same time.
4. Any failure that is not a certain refusal makes the outcome uncertain. Then the command is
   looked up by its id; if it was not applied, it is attempted once more with the same key,
   which the claims store's UNIQUE constraint makes harmless.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from cortex.memory.ledger import OperationStatus, PendingAction, PendingStatus, TenantLedger
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
    code: Literal["operation_id_conflict", "operation_in_progress"]
    detail: str


@dataclass(frozen=True, slots=True)
class Failed:
    result: ToolResult


@dataclass(frozen=True, slots=True)
class Unresolved:
    operation_id: str


WriteOutcome = Done | NeedsConfirmation | Refused | Failed | Unresolved


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
        if existing.status is OperationStatus.UNCERTAIN:
            return _reconcile(approved, adapter, context, operation_id)
        if existing.status is OperationStatus.STARTED:
            return Refused("operation_in_progress", "another run is executing this command")
        # FAILED: refused for sure, nothing was applied; it may be confirmed and tried again.

    pending = ledger.pending_action(operation_id)
    if pending is not None and pending.conversation_id != context.conversation_id:
        return Refused("operation_id_conflict", "the id belongs to another conversation")
    if not is_confirmed(pending, values, context.control_version):
        ledger.record_pending(
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
        return NeedsConfirmation(operation_id, confirmation_question(name, arguments))

    if not ledger.start_operation(operation_id, name, values, context.run_id):
        return Refused("operation_in_progress", "another run is executing this command")
    return _attempt(approved, adapter, context, operation_id, retry_if_uncertain=True)


def _attempt(
    approved: Approved,
    adapter: WriteAdapter,
    context: WriteContext,
    operation_id: str,
    *,
    retry_if_uncertain: bool,
) -> WriteOutcome:
    name = approved.spec.name
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
        if not retry_if_uncertain:
            return Unresolved(operation_id)
        return _reconcile(approved, adapter, context, operation_id)
    return _commit(name, adapter, context, operation_id, data, "executed")


def _reconcile(
    approved: Approved, adapter: WriteAdapter, context: WriteContext, operation_id: str
) -> WriteOutcome:
    name = approved.spec.name
    try:
        found = adapter.lookup(operation_id)
    except DomainError:
        context.trace("write_lookup", tool=name, found=None)  # it could not even be checked
        return Unresolved(operation_id)
    context.trace("write_lookup", tool=name, found=found is not None)
    if found is not None:
        return _commit(approved.spec.name, adapter, context, operation_id, found, "reconciled")
    # Not applied: one more attempt with the same key. If the first attempt commits late, the
    # store's UNIQUE key turns this one into a read of that claim.
    outcome = _attempt(approved, adapter, context, operation_id, retry_if_uncertain=False)
    if isinstance(outcome, Done):
        return Done(outcome.result, "reconciled")
    return outcome


def _commit(
    name: str,
    adapter: WriteAdapter,
    context: WriteContext,
    operation_id: str,
    data: Mapping[str, object],
    how: Literal["executed", "reconciled"],
) -> Done:
    context.ledger.settle_operation(
        operation_id,
        OperationStatus.COMMITTED,
        json.dumps(dict(data), ensure_ascii=False),
        effect_in=context.conversation_id,
    )
    context.ledger.move_pending(
        operation_id, from_status=PendingStatus.CONFIRMED, to_status=PendingStatus.CONSUMED
    )
    return Done(_ok(name, adapter, data), how)


def _ok(name: str, adapter: WriteAdapter, data: Mapping[str, object]) -> ToolResult:
    return ToolResult(name, ok=True, source_tenant=adapter.tenant_id, data=data)


def _operation_id(arguments: object) -> str:
    operation_id = getattr(arguments, "operation_id", None)
    if not isinstance(operation_id, str):
        raise TypeError("a write tool's arguments must carry an operation_id")
    return operation_id
