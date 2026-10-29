"""Trusted identity (brief section 3): who is calling and for which company.

The company (tenant) and the principal come from the server's trusted context, never from what
the user writes or from the model's tool arguments. An `AuthContext` can only be obtained from
`PrincipalDirectory.authenticate`, so any code that holds one knows it was checked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar

from merchant_assistant.shared.errors import DomainError

# Identifiers end up in SQL parameters, file names and traces: a closed alphabet keeps them inert.
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_]{0,63}", re.ASCII)

_PROOF = object()


class AuthenticationFailed(DomainError):  # noqa: N818 - named after the situation
    """The principal does not belong to the company it claims, or one of them is unknown."""

    code: ClassVar[str] = "auth_invalid"
    user_message: ClassVar[str] = "The request could not be authenticated."


@dataclass(frozen=True, slots=True)
class AuthContext:
    """An authenticated principal acting for one company, with its permissions."""

    tenant_id: str
    principal_id: str
    permissions: frozenset[str]
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _PROOF:
            raise TypeError("an AuthContext is created only by PrincipalDirectory.authenticate")

    def can(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    tenant_id: str
    principal_id: str
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.principal_id):
            if IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"not a valid identifier: {value!r}")


class PrincipalDirectory:
    """The server-side list of who belongs to which company. Read-only after construction."""

    def __init__(self, records: Iterable[PrincipalRecord]) -> None:
        by_key: dict[tuple[str, str], PrincipalRecord] = {}
        for record in records:
            key = (record.tenant_id, record.principal_id)
            if key in by_key:
                raise ValueError(f"duplicate principal: {key}")
            by_key[key] = record
        self._records: Mapping[tuple[str, str], PrincipalRecord] = MappingProxyType(by_key)

    @property
    def tenants(self) -> frozenset[str]:
        return frozenset(tenant for tenant, _ in self._records)

    def authenticate(self, tenant_id: str, principal_id: str) -> AuthContext:
        record = self._records.get((tenant_id, principal_id))
        if record is None:
            # One error for every case: a caller learns nothing about which part was wrong.
            raise AuthenticationFailed(f"no principal {principal_id!r} in tenant {tenant_id!r}")
        return AuthContext(
            tenant_id=record.tenant_id,
            principal_id=record.principal_id,
            permissions=record.permissions,
            _proof=_PROOF,
        )
