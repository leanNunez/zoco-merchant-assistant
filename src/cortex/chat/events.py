"""The two Zoco Chat events the harness understands, read strictly (brief section 6).

- `operator_took_over`: a person took the conversation, at a control version.
- `followup_due`: a follow-up of an opportunity is due and needs a decision.

The company and the conversation an event names must be the trusted ones of the call: an event
for another company is refused, never applied. Keys that start with `expected` (the kit's
fixtures carry them) are dropped unread; any other unknown key is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import ClassVar, Literal

from merchant_assistant.sales.contract import parse_iso_day
from merchant_assistant.shared.errors import DomainError

TAKEOVER = "operator_took_over"
FOLLOWUP = "followup_due"

_COMMON = frozenset({"type", "id", "tenant_id", "conversation_id", "timestamp"})
_TAKEOVER_KEYS = _COMMON | {"version", "actor"}
_FOLLOWUP_KEYS = _COMMON | {
    "opportunity_id",
    "responsible",
    "pending_on",
    "contact_allowed",
    "opted_out",
    "opportunity_status",
    "agreed_contact_date",
    "customer_replied",
}


class InvalidEvent(DomainError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "invalid_event"
    user_message: ClassVar[str] = "The event is malformed."


class EventOutOfScope(DomainError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "scope_violation"
    user_message: ClassVar[str] = "The event belongs to another company or conversation."


@dataclass(frozen=True, slots=True)
class Takeover:
    version: int
    actor: str | None


@dataclass(frozen=True, slots=True)
class FollowupFacts:
    pending_on: Literal["ZOCO", "CUSTOMER"] | None = None
    responsible: str | None = None
    contact_allowed: bool | None = None  # None: consent unknown, which never allows contact
    opted_out: bool = False
    opportunity_status: str | None = None
    agreed_contact_date: date | None = None
    customer_replied: bool = False


def event_type(raw: Mapping[str, object]) -> str:
    kind = raw.get("type")
    if kind not in (TAKEOVER, FOLLOWUP):
        raise InvalidEvent(f"unsupported event type {kind!r}")
    return str(kind)


def read_takeover(raw: Mapping[str, object], *, tenant_id: str, conversation_id: str) -> Takeover:
    data = _checked(raw, _TAKEOVER_KEYS, tenant_id, conversation_id)
    version = data.get("version")
    if type(version) is not int or version < 1:
        raise InvalidEvent("a takeover needs a positive integer version")
    return Takeover(version=version, actor=_text(data, "actor"))


def read_followup(
    raw: Mapping[str, object], *, tenant_id: str, conversation_id: str
) -> FollowupFacts:
    data = _checked(raw, _FOLLOWUP_KEYS, tenant_id, conversation_id)
    pending_on: Literal["ZOCO", "CUSTOMER"] | None
    match data.get("pending_on"):
        case None:
            pending_on = None
        case "ZOCO":
            pending_on = "ZOCO"
        case "CUSTOMER":
            pending_on = "CUSTOMER"
        case _:
            raise InvalidEvent("pending_on must be ZOCO or CUSTOMER")
    agreed = _text(data, "agreed_contact_date")
    status = _text(data, "opportunity_status")
    try:
        agreed_date = parse_iso_day(agreed) if agreed is not None else None
    except DomainError:
        raise InvalidEvent("agreed_contact_date must be YYYY-MM-DD") from None
    return FollowupFacts(
        pending_on=pending_on,
        responsible=_text(data, "responsible"),
        contact_allowed=_flag(data, "contact_allowed"),
        opted_out=_flag(data, "opted_out") is True,
        opportunity_status=status.upper() if status else None,
        agreed_contact_date=agreed_date,
        customer_replied=_flag(data, "customer_replied") is True,
    )


def _checked(
    raw: Mapping[str, object], allowed: frozenset[str], tenant_id: str, conversation_id: str
) -> dict[str, object]:
    data = {key: value for key, value in raw.items() if not key.startswith("expected")}
    unknown = set(data) - allowed
    if unknown:
        raise InvalidEvent(f"unknown event keys: {sorted(unknown)}")
    for key, trusted in (("tenant_id", tenant_id), ("conversation_id", conversation_id)):
        if key in data and data[key] != trusted:
            raise EventOutOfScope(f"the event names {key}={data[key]!r}")
    return data


def _text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidEvent(f"{key} must be a non-empty string")
    return value


def _flag(data: Mapping[str, object], key: str) -> bool | None:
    value = data.get(key)
    if value is not None and type(value) is not bool:
        raise InvalidEvent(f"{key} must be true or false")
    return value
