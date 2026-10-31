"""`crear_reclamo` over the first delivery's complaints store, unchanged, one file per company.

The complaints store plays the external claims system: a separate database with no shared
transaction with the harness, which is what makes an uncertain outcome realistic. Each company
has its own file, so a claim cannot even be looked up across companies. The idempotency key is
derived from the company and the `operation_id`, and the store's UNIQUE constraint guarantees
that repeating the command never files a second claim.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.tools.failures import UncertainOutcome
from cortex.tools.zoco_catalog import CrearReclamoOperationArgs
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.errors import ComplaintStoreError
from merchant_assistant.complaints.models import Complaint, ComplaintDraft
from merchant_assistant.shared.clock import Clock


def claim_key(tenant_id: str, operation_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}\n{operation_id}".encode()).hexdigest()


class ClaimsDesk:
    """Opens the claims file of each company inside one directory."""

    def __init__(self, directory: Path, clock: Clock) -> None:
        self.directory = directory
        self._clock = clock

    def path_for(self, tenant_id: str) -> Path:
        # Safe as a file name: tenant ids come from an AuthContext, whose alphabet is closed.
        return self.directory / f"{tenant_id}.db"

    def for_tenant(self, auth: AuthContext) -> ClaimsAdapter:
        repository = SqliteComplaintRepository(self.path_for(auth.tenant_id))
        repository.initialize()
        return ClaimsAdapter(auth.tenant_id, repository, self._clock)


class ClaimsAdapter:
    def __init__(self, tenant_id: str, repository: SqliteComplaintRepository, clock: Clock) -> None:
        self.tenant_id = tenant_id
        self._repository = repository
        self._clock = clock

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        if not isinstance(arguments, CrearReclamoOperationArgs):
            raise TypeError("crear_reclamo received arguments of another tool")
        try:
            outcome = self._repository.create(
                ComplaintDraft(arguments.motivo, arguments.descripcion),
                conversation_id=conversation_id,
                key=claim_key(self.tenant_id, arguments.operation_id),
                now=self._clock.now(),
            )
        except ComplaintStoreError as failure:
            # The store's own contract: when it fails, whether the claim was saved is unknown.
            raise UncertainOutcome(failure.detail) from failure
        return _view(outcome.complaint, arguments.operation_id)

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        try:
            complaint = self._repository.find_by_key(claim_key(self.tenant_id, operation_id))
        except ComplaintStoreError as failure:
            raise UncertainOutcome(failure.detail) from failure
        return None if complaint is None else _view(complaint, operation_id)


def _view(complaint: Complaint, operation_id: str) -> Mapping[str, object]:
    return {
        "reclamo_id": complaint.id,
        "operation_id": operation_id,
        "motivo": complaint.draft.reason,
        "estado": "registrado",
    }
