"""Reading a kit scenario: what is dropped on load, and what is refused."""

import json
from datetime import UTC

import pytest

from cortex.chat.events import InvalidEvent, read_followup
from cortex.kit_adapter.scenario import UnsupportedScenario, load_scenario

BASE = {"tenant_id": "comercio_a", "principal_id": "operador_a", "conversation_id": "conv_1"}
SCOPE = {"tenant_id": "comercio_a", "conversation_id": "conv_1"}


def _scenario(event: object) -> dict[str, object]:
    return {**BASE, "model_script": [{"final": "OK"}], "incoming_event": event}


def test_the_expected_keys_of_the_kits_events_are_dropped_when_the_scenario_is_loaded() -> None:
    event = {
        "type": "followup_due",
        "pending_on": "ZOCO",
        "expected_decision": "CONTACTAR",
        "expected": {"anything": 1},
    }

    loaded = load_scenario(_scenario(event)).incoming_event

    assert loaded == {"type": "followup_due", "pending_on": "ZOCO"}
    assert "expected_decision" in event  # the caller's own dictionary was not changed


def test_an_expected_key_that_reaches_the_event_reader_is_refused_as_unknown() -> None:
    with pytest.raises(InvalidEvent, match="unknown event keys"):
        read_followup(
            {"type": "followup_due", "expected_decision": "CONTACTAR"}, **SCOPE, business_tz=UTC
        )


def test_an_event_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(UnsupportedScenario, match="incoming_event"):
        load_scenario(_scenario(["followup_due"]))


def test_a_scenario_without_an_event_has_none() -> None:
    assert load_scenario(_scenario(None)).incoming_event is None


LONE_SURROGATE = json.loads('"a\\ud800b"')  # valid JSON, but no UTF-8 can encode it


@pytest.mark.parametrize(
    "changes",
    [
        {"message": LONE_SURROGATE},
        {"model_script": [{"final": LONE_SURROGATE}]},
        {"preconditions": {"history": [{"text": LONE_SURROGATE}]}},
        {"preconditions": {LONE_SURROGATE: 1}},
    ],
)
def test_a_string_that_utf8_cannot_encode_is_refused_wherever_it_is(
    changes: dict[str, object],
) -> None:
    with pytest.raises(UnsupportedScenario, match="UTF-8") as refused:
        load_scenario({**_scenario(None), **changes})

    str(refused.value).encode("utf-8")  # the reason itself can be written anywhere
