"""Verification by code of every tool request (brief section 3). Pure: no I/O.

Deny by default. The checks run in a fixed order and the first refusal wins; a refused request
never reaches the tool's adapter.

1. The tool exists in the catalog and the conversation mode allows its kind.
2. Scope guard: the arguments may not name a company, a principal or a role. The company comes
   from the trusted context only. This check runs before the schema so the reason is specific,
   and it ends the run: choosing another company is an attack, not a typo.
3. The principal holds the tool's permission.
4. The arguments pass the strict schema.
5. The tool's domain precheck passes (for example, no future sales date).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.tools.catalog import Catalog, ToolKind, ToolSpec
from cortex.tools.schema_guard import validate_arguments
from merchant_assistant.agent.domain.errors import InvalidToolArguments
from merchant_assistant.agent.domain.guards import MAX_ARGUMENTS_CHARS
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.capabilities import capabilities_for
from merchant_assistant.control.states import State
from merchant_assistant.shared.errors import DomainError

SCOPE_FIELDS = frozenset(
    {
        "tenant_id",
        "tenant",
        "empresa",
        "company",
        "comercio",
        "merchant_id",
        "principal_id",
        "role",
        "rol",
        "is_admin",
        "admin",
    }
)

# A tool name the model invented is untrusted text: it is shown only when it is a plain name.
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
REDACTED_NAME = "<redacted>"


class RejectionCode(StrEnum):
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    SCOPE_VIOLATION = "scope_violation"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGUMENTS = "invalid_arguments"


@dataclass(frozen=True, slots=True)
class Approved:
    spec: ToolSpec
    arguments: BaseModel


@dataclass(frozen=True, slots=True)
class Rejected:
    tool: str
    code: RejectionCode
    detail: str  # field names and problem codes only, never a value

    @property
    def ends_run(self) -> bool:
        return self.code is RejectionCode.SCOPE_VIOLATION


@dataclass(frozen=True, slots=True)
class PolicyContext:
    auth: AuthContext
    mode: State
    today: date


def display_name(name: str) -> str:
    return name if _PLAIN_NAME.fullmatch(name) else REDACTED_NAME


def review(call: ToolCall, catalog: Catalog, context: PolicyContext) -> Approved | Rejected:
    tool = display_name(call.name)
    spec = catalog.get(call.name)
    if spec is None or not _mode_allows(context.mode, spec):
        return Rejected(tool, RejectionCode.TOOL_NOT_ALLOWED, "not offered in this conversation")

    scope_fields = _argument_names(call.arguments) & SCOPE_FIELDS
    if scope_fields:
        return Rejected(tool, RejectionCode.SCOPE_VIOLATION, f"fields: {sorted(scope_fields)}")

    if not context.auth.can(spec.permission):
        return Rejected(tool, RejectionCode.PERMISSION_DENIED, f"requires {spec.permission}")

    try:
        arguments = validate_arguments(spec, call)
    except InvalidToolArguments as invalid:
        return Rejected(tool, RejectionCode.INVALID_ARGUMENTS, invalid.public()["issues"])

    if spec.precheck is not None:
        try:
            spec.precheck(arguments, context.today)
        except DomainError as refused:
            return Rejected(tool, RejectionCode.INVALID_ARGUMENTS, refused.code)
    return Approved(spec, arguments)


def _mode_allows(mode: State, spec: ToolSpec) -> bool:
    # The harness has no attention hours (brief 2 does not define them), so they are always open.
    capabilities = capabilities_for(mode, hours_open=True)
    if spec.kind is ToolKind.READ:
        return capabilities.read_tools
    return capabilities.effect_tools and not capabilities.effects_need_operator_approval


def _argument_names(raw: str) -> frozenset[str]:
    """The top-level keys, if the payload is a JSON object. The schema check handles the rest."""
    if len(raw) > MAX_ARGUMENTS_CHARS:
        return frozenset()
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return frozenset()
    return (
        frozenset(str(key).casefold() for key in parsed)
        if isinstance(parsed, dict)
        else frozenset()
    )
