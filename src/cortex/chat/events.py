"""The two Zoco Chat events the harness understands, read strictly (brief section 6).

- `operator_took_over` (also `operator_takeover`): a person took the conversation, at a
  control version.
- `followup_due`: a follow-up of an opportunity is due and needs a decision.

The company an event names must be the trusted one of the call: an event for another company is
refused, never applied. A conversation it names is the one it is about: when the company is the
same, the caller acts on that conversation (`named_conversation`), and reading the event with any
other conversation is refused. Any unknown key is refused (the `expected...` keys the kit's
fixtures carry are dropped by the kit adapter when it loads the scenario, never seen here).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import ClassVar, Literal

from cortex.config import MAX_CONTROL_VERSION
from cortex.tools.policy import RejectionCode
from merchant_assistant.sales.contract import parse_iso_day
from merchant_assistant.shared.errors import DomainError

TAKEOVER = "operator_took_over"
FOLLOWUP = "followup_due"
# Other names the kit's documents give to the same events.
_ALIASES: Mapping[str, str] = MappingProxyType({"operator_takeover": TAKEOVER})

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
    code: ClassVar[str] = RejectionCode.SCOPE_VIOLATION.value
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

    def digest(self) -> str:
        """A fingerprint of the event's facts: the same event always gives the same digest."""
        facts = {name: getattr(self, name) for name in self.__dataclass_fields__}
        text = json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_type(raw: Mapping[str, object]) -> str:
    """The event's type, by its canonical name."""
    kind = raw.get("type")
    kind = _ALIASES.get(kind, kind) if isinstance(kind, str) else kind
    if kind not in (TAKEOVER, FOLLOWUP):
        raise InvalidEvent(f"unsupported event type {kind!r}")
    return str(kind)


def named_conversation(raw: Mapping[str, object], *, tenant_id: str) -> str | None:
    """The conversation the event names, if it names one and the company is the trusted one.

    An event of another company names nothing the caller may act on: it stays out of scope.
    """
    conversation = raw.get("conversation_id")
    if raw.get("tenant_id", tenant_id) != tenant_id or not isinstance(conversation, str):
        return None
    return conversation


def read_takeover(raw: Mapping[str, object], *, tenant_id: str, conversation_id: str) -> Takeover:
    data = _checked(raw, _TAKEOVER_KEYS, tenant_id, conversation_id)
    version = data.get("version")
    if type(version) is not int or not 1 <= version <= MAX_CONTROL_VERSION:
        raise InvalidEvent(
            f"a takeover needs an integer version between 1 and {MAX_CONTROL_VERSION}"
        )
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
    data = dict(raw)
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
