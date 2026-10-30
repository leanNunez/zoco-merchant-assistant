import pytest

from cortex.chat.events import (
    EventOutOfScope,
    InvalidEvent,
    event_type,
    read_followup,
    read_takeover,
)

SCOPE = {"tenant_id": "comercio_a", "conversation_id": "conv_1"}


def test_the_kits_expected_keys_are_dropped_unread() -> None:
    facts = read_followup(
        {
            "type": "followup_due",
            "pending_on": "ZOCO",
            "expected_decision": "CONTACTAR",
            "expected": "anything",
        },
        **SCOPE,
    )

    assert facts.pending_on == "ZOCO"


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(InvalidEvent, match="unknown event keys"):
        read_followup({"type": "followup_due", "force_contact": True}, **SCOPE)


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


@pytest.mark.parametrize("version", [0, -1, "2", True, None])
def test_a_takeover_needs_a_positive_integer_version(version: object) -> None:
    with pytest.raises(InvalidEvent):
        read_takeover({"type": "operator_took_over", "version": version}, **SCOPE)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "followup_due", "agreed_contact_date": "06/11/2026"},
        {"type": "followup_due", "pending_on": "NOBODY"},
        {"type": "followup_due", "contact_allowed": "yes"},
    ],
)
def test_malformed_follow_up_facts_are_refused(raw: dict[str, object]) -> None:
    with pytest.raises(InvalidEvent):
        read_followup(raw, **SCOPE)


def test_only_the_two_known_events_are_accepted() -> None:
    assert event_type({"type": "operator_took_over"}) == "operator_took_over"
    with pytest.raises(InvalidEvent):
        event_type({"type": "delete_conversation"})


def test_the_kits_other_name_for_a_takeover_is_the_same_event() -> None:
    assert event_type({"type": "operator_takeover"}) == event_type({"type": "operator_took_over"})
