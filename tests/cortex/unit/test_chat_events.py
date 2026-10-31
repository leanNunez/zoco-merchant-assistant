import json
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from cortex.chat.events import (
    EventOutOfScope,
    InvalidEvent,
    event_type,
    named_conversation,
    read_followup,
    read_takeover,
)
from cortex.config import MAX_CONTROL_VERSION

SCOPE = {"tenant_id": "comercio_a", "conversation_id": "conv_1"}
BUSINESS_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(InvalidEvent, match="unknown event keys"):
        read_followup(
            {"type": "followup_due", "force_contact": True}, **SCOPE, business_tz=BUSINESS_TZ
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "operator_took_over", "version": 2, "tenant_id": "comercio_b"},
        {"type": "operator_took_over", "version": 2, "conversation_id": "conv_9"},
    ],
)
def test_an_event_for_another_company_or_conversation_is_refused(raw: dict[str, object]) -> None:
    with pytest.raises(EventOutOfScope):
        read_takeover(raw, **SCOPE)


def test_a_takeover_version_is_bounded_above() -> None:
    event = {"type": "operator_took_over"}

    assert read_takeover({**event, "version": MAX_CONTROL_VERSION}, **SCOPE).version == (
        MAX_CONTROL_VERSION
    )
    for version in (MAX_CONTROL_VERSION + 1, 2**70):
        with pytest.raises(InvalidEvent, match="version"):
            read_takeover({**event, "version": version}, **SCOPE)


@pytest.mark.parametrize("version", [0, -1, "2", True, None])
def test_a_takeover_needs_a_positive_integer_version(version: object) -> None:
    with pytest.raises(InvalidEvent):
        read_takeover({"type": "operator_took_over", "version": version}, **SCOPE)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "followup_due", "pending_on": "NOBODY"},
        {"type": "followup_due", "contact_allowed": "yes"},
        {"type": "followup_due", "opportunity_id": "opp a; DROP"},
        {"type": "followup_due", "opportunity_id": "o" * 200},
    ],
)
def test_malformed_follow_up_facts_are_refused(raw: dict[str, object]) -> None:
    with pytest.raises(InvalidEvent):
        read_followup(raw, **SCOPE, business_tz=BUSINESS_TZ)


def test_only_the_two_known_events_are_accepted() -> None:
    assert event_type({"type": "operator_took_over"}) == "operator_took_over"
    with pytest.raises(InvalidEvent):
        event_type({"type": "delete_conversation"})


def test_the_kits_other_name_for_a_takeover_is_the_same_event() -> None:
    assert event_type({"type": "operator_takeover"}) == event_type({"type": "operator_took_over"})


@pytest.mark.parametrize(
    ("raw", "named"),
    [
        ({"type": "followup_due", "conversation_id": "conv_9"}, "conv_9"),
        (
            {"type": "followup_due", "tenant_id": "comercio_a", "conversation_id": "conv_9"},
            "conv_9",
        ),
        ({"type": "followup_due", "tenant_id": "comercio_b", "conversation_id": "conv_9"}, None),
        ({"type": "followup_due", "tenant_id": "comercio_a"}, None),
        ({"type": "followup_due", "conversation_id": 9}, None),
    ],
)
def test_an_event_names_a_conversation_only_inside_the_trusted_company(
    raw: dict[str, object], named: str | None
) -> None:
    assert named_conversation(raw, tenant_id="comercio_a") == named


def test_a_follow_up_keeps_its_opportunity_and_the_digest_covers_every_fact() -> None:
    event = {"type": "followup_due", "pending_on": "ZOCO"}
    first = read_followup({**event, "opportunity_id": "opp_a"}, **SCOPE, business_tz=BUSINESS_TZ)
    second = read_followup({**event, "opportunity_id": "opp_b"}, **SCOPE, business_tz=BUSINESS_TZ)

    assert first.opportunity_id == "opp_a"
    # The same facts about another opportunity are another event, not a repetition.
    assert first.digest() != second.digest()
    assert read_followup(event, **SCOPE, business_tz=BUSINESS_TZ).opportunity_id is None
    assert set(json.loads(first.canonical())) == set(first.__dataclass_fields__)


@pytest.mark.parametrize(
    ("agreed", "day"),
    [
        ("2026-11-06", date(2026, 11, 6)),
        ("2026-11-06T10:00:00-03:00", date(2026, 11, 6)),
        # 02:00 in UTC is still the evening before in Buenos Aires: the business date counts.
        ("2026-11-06T02:00:00+00:00", date(2026, 11, 5)),
        ("2026-11-06T02:00:00Z", date(2026, 11, 5)),
    ],
)
def test_an_agreed_date_is_a_day_or_a_moment_with_an_offset(agreed: str, day: date) -> None:
    raw = {"type": "followup_due", "agreed_contact_date": agreed}

    facts = read_followup(raw, **SCOPE, business_tz=BUSINESS_TZ)

    assert (facts.agreed_contact_date, facts.agreed_date_unreadable) == (day, False)


@pytest.mark.parametrize(
    "agreed", ["06/11/2026", "2026-02-30", "2026-11-06T10:00:00", "mañana", "", 20261106]
)
def test_an_agreed_date_that_cannot_be_read_is_kept_as_unreadable(agreed: object) -> None:
    raw = {"type": "followup_due", "agreed_contact_date": agreed}

    facts = read_followup(raw, **SCOPE, business_tz=BUSINESS_TZ)

    assert (facts.agreed_contact_date, facts.agreed_date_unreadable) == (None, True)


@pytest.mark.parametrize("agreed", ["9999-12-31T23:59:59-03:00", "0001-01-01T00:00:00+23:59"])
def test_an_agreed_moment_outside_the_representable_range_is_unreadable(agreed: str) -> None:
    raw = {"type": "followup_due", "agreed_contact_date": agreed}

    facts = read_followup(raw, **SCOPE, business_tz=BUSINESS_TZ)

    assert (facts.agreed_contact_date, facts.agreed_date_unreadable) == (None, True)


@pytest.mark.parametrize("raw", ["OPERATOR_TOOK_OVER", "Operator_Takeover", "FollowUp_Due"])
def test_the_event_type_is_read_in_any_letter_case(raw: str) -> None:
    assert event_type({"type": raw}) in {"operator_took_over", "followup_due"}


@pytest.mark.parametrize(("raw", "pending_on"), [("zoco", "ZOCO"), ("Customer", "CUSTOMER")])
def test_pending_on_is_read_in_any_letter_case(raw: str, pending_on: str) -> None:
    event = {"type": "followup_due", "pending_on": raw}

    assert read_followup(event, **SCOPE, business_tz=BUSINESS_TZ).pending_on == pending_on
