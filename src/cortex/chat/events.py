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
from datetime import date, datetime, tzinfo
from types import MappingProxyType
from typing import ClassVar, Literal

from cortex.config import MAX_CONTROL_VERSION
from cortex.identifiers import EXTERNAL_ID
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
    # The event gave an agreed date that is not a day nor a moment with an offset (06/11/2026,
    # 2026-02-30): it says nothing reliable, so it can never reschedule.
    agreed_date_unreadable: bool = False
    customer_replied: bool = False
    # The opportunity the follow-up is about: what is owed is kept per opportunity.
    opportunity_id: str | None = None

    def canonical(self) -> str:
        """Every fact of the event, as canonical JSON."""
        facts = {name: getattr(self, name) for name in self.__dataclass_fields__}
        return json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))

    def digest(self) -> str:
        """A fingerprint of the event's facts: the same event always gives the same digest."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def event_type(raw: Mapping[str, object]) -> str:
    """The event's type, by its canonical name, in any letter case."""
    kind = raw.get("type")
    if isinstance(kind, str):
        kind = _ALIASES.get(kind.lower(), kind.lower())
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
    # 0 is the stored default: a report at 0 is read, and the store finds it stale or not.
    if type(version) is not int or not 0 <= version <= MAX_CONTROL_VERSION:
        raise InvalidEvent(
            f"a takeover needs an integer version between 0 and {MAX_CONTROL_VERSION}"
        )
    return Takeover(version=version, actor=_text(data, "actor"))


def read_followup(
    raw: Mapping[str, object], *, tenant_id: str, conversation_id: str, business_tz: tzinfo
) -> FollowupFacts:
    """`business_tz` turns an agreed moment (a datetime with its offset) into a business date."""
    data = _checked(raw, _FOLLOWUP_KEYS, tenant_id, conversation_id)
    pending_on: Literal["ZOCO", "CUSTOMER"] | None
    raw_pending_on = data.get("pending_on")
    match raw_pending_on.upper() if isinstance(raw_pending_on, str) else raw_pending_on:
        case None:
            pending_on = None
        case "ZOCO":
            pending_on = "ZOCO"
        case "CUSTOMER":
            pending_on = "CUSTOMER"
        case _:
            raise InvalidEvent("pending_on must be ZOCO or CUSTOMER")
    status = _text(data, "opportunity_status")
    agreed_date, unreadable = _agreed_date(data.get("agreed_contact_date"), business_tz)
    return FollowupFacts(
        pending_on=pending_on,
        responsible=_text(data, "responsible"),
        contact_allowed=_flag(data, "contact_allowed"),
        opted_out=_flag(data, "opted_out") is True,
        opportunity_status=status.upper() if status else None,
        agreed_contact_date=agreed_date,
        agreed_date_unreadable=unreadable,
        customer_replied=_flag(data, "customer_replied") is True,
        opportunity_id=_identifier(data, "opportunity_id"),
    )


def _agreed_date(value: object, business_tz: tzinfo) -> tuple[date | None, bool]:
    """(the agreed contact date, whether it was unreadable). A day (YYYY-MM-DD), or a moment with
    its offset (ISO 8601) taken as the business date it falls on. Anything else is unreadable,
    never a guess."""
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    try:
        return parse_iso_day(value), False
    except DomainError:
        pass
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None, True
    if moment.utcoffset() is None:
        return None, True  # a moment without an offset could be any day
    try:
        return moment.astimezone(business_tz).date(), False
    except OverflowError:
        return None, True  # at the edge of the calendar the business day cannot be represented


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


def _identifier(data: Mapping[str, object], key: str) -> str | None:
    """An id from Zoco Chat, held to the closed alphabet and cap of every external id."""
    value = _text(data, key)
    if value is not None and EXTERNAL_ID.fullmatch(value) is None:
        raise InvalidEvent(f"{key} is not a valid id")
    return value


def _flag(data: Mapping[str, object], key: str) -> bool | None:
    value = data.get(key)
    if value is not None and type(value) is not bool:
        raise InvalidEvent(f"{key} must be true or false")
    return value
