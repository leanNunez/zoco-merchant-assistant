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
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from cortex.identifiers import PLAIN_TOOL_NAME
from cortex.identity import AuthContext
from cortex.tools.catalog import Catalog, ToolKind, ToolSpec
from cortex.tools.schema_guard import validate_arguments
from merchant_assistant.agent.domain.errors import InvalidToolArguments
from merchant_assistant.agent.domain.guards import MAX_ARGUMENTS_CHARS
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.control.capabilities import capabilities_for
from merchant_assistant.control.states import State
from merchant_assistant.shared.errors import DomainError

# What the arguments may never name: who is asking or for which company. The names are compared
# in a normal form (see `normalize_key`), so `tenantId`, `Tenant-ID` and `tenant_id` are one
# field. The list stays in code on purpose: a configuration file could loosen it.
SCOPE_BASES = frozenset(
    {
        "tenant",
        "empresa",
        "company",
        "comercio",
        "merchant",
        "principal",
        "role",
        "rol",
        "admin",
        "isadmin",
    }
)
# A normalized key is a scope field when it is a base name or a base name followed by "id".
SCOPE_FIELDS = SCOPE_BASES | frozenset(f"{base}id" for base in SCOPE_BASES)

# A tool name the model invented is untrusted text: it is shown only when it is a plain name.
REDACTED_NAME = "<redacted>"


class RejectionCode(StrEnum):
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    SCOPE_VIOLATION = "scope_violation"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    # Raised by the write path, after the policy, when the ledger refuses the command.
    OPERATION_ID_CONFLICT = "operation_id_conflict"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    CONFIRMATION_MISSING = "confirmation_missing"


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
    return name if PLAIN_TOOL_NAME.fullmatch(name) else REDACTED_NAME


def review(call: ToolCall, catalog: Catalog, context: PolicyContext) -> Approved | Rejected:
    tool = display_name(call.name)
    spec = catalog.get(call.name)
    if spec is None or not _mode_allows(context.mode, spec):
        return Rejected(tool, RejectionCode.TOOL_NOT_ALLOWED, "not offered in this conversation")

    scope_fields = _scope_fields_named(call.arguments)
    if scope_fields:
        return Rejected(tool, RejectionCode.SCOPE_VIOLATION, f"fields: {scope_fields}")

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


def normalize_key(key: str) -> str:
    """The key in the form it is compared in: NFKC (fullwidth letters become plain ones), case
    folded, and only letters and digits left, so separators and invisible characters do not
    make a different name."""
    folded = unicodedata.normalize("NFKC", key).casefold()
    return "".join(character for character in folded if character.isalnum())


def _scope_fields_named(raw: str) -> list[str]:
    """The scope fields the payload names at any depth, in their normal form, sorted.

    Only the normal form is returned, never the raw key: the model chose that text. The walk is
    iterative, so a deeply nested payload cannot exhaust the stack. A payload that is not JSON
    is left to the schema check.
    """
    if len(raw) > MAX_ARGUMENTS_CHARS:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    found: set[str] = set()
    pending = [parsed]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            found.update(name for name in map(normalize_key, node) if name in SCOPE_FIELDS)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return sorted(found)
