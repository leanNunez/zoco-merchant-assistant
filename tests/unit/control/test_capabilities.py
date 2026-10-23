import pytest

from merchant_assistant.control.capabilities import Capabilities, Delivery, capabilities_for
from merchant_assistant.control.states import State

# (state, hours_open) -> (delivery, generate_reply, read_tools, effect_tools,
#                         effects_need_operator_approval, inform_hours_window)
TABLE_OF_RECORD = {
    (State.AUTO, True): (Delivery.SEND, True, True, True, False, False),
    (State.AUTO, False): (Delivery.SEND, True, True, True, False, True),
    (State.SUGGEST, True): (Delivery.DRAFT, True, True, True, True, False),
    (State.SUGGEST, False): (Delivery.DRAFT, True, True, True, True, True),
    (State.HUMAN, True): (Delivery.NONE, False, False, False, False, False),
    (State.HUMAN, False): (Delivery.NONE, False, False, False, False, False),
    (State.BLOCKED, True): (Delivery.NONE, False, False, False, False, False),
    (State.BLOCKED, False): (Delivery.NONE, False, False, False, False, False),
}


@pytest.mark.parametrize(
    ("state", "hours_open"),
    [pytest.param(s, o, id=f"{s.value}-{'open' if o else 'closed'}") for s, o in TABLE_OF_RECORD],
)
def test_what_the_agent_may_do_in_each_state_and_hour(state: State, hours_open: bool) -> None:
    expected = Capabilities(*TABLE_OF_RECORD[(state, hours_open)])

    assert capabilities_for(state, hours_open=hours_open) == expected


def test_the_table_of_record_covers_every_state_with_open_and_closed_hours() -> None:
    assert {state for state, _ in TABLE_OF_RECORD} == set(State)
    assert len(TABLE_OF_RECORD) == 8


@pytest.mark.parametrize("state", [State.HUMAN, State.BLOCKED])
def test_in_human_and_blocked_the_agent_has_no_tools_at_all(state: State) -> None:
    capabilities = capabilities_for(state, hours_open=True)

    assert not capabilities.read_tools
    assert not capabilities.effect_tools


def test_a_suggested_reply_is_never_sent_without_approval() -> None:
    assert capabilities_for(State.SUGGEST, hours_open=True).delivery is Delivery.DRAFT


def test_a_complaint_needs_the_operator_only_while_the_operator_reviews() -> None:
    needing_approval = {
        state
        for state in State
        if capabilities_for(state, hours_open=True).effects_need_operator_approval
    }

    assert needing_approval == {State.SUGGEST}


def test_capabilities_are_immutable() -> None:
    capabilities = capabilities_for(State.AUTO, hours_open=True)

    with pytest.raises(AttributeError):
        capabilities.read_tools = False  # type: ignore[misc]
