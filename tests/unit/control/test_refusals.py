import pytest

from merchant_assistant.control.errors import GuardRejected, InvalidTransition, UnauthorizedActor
from merchant_assistant.control.machine import transition
from merchant_assistant.control.states import Actor, Event, State
from merchant_assistant.shared.errors import DomainError


def test_each_refusal_has_its_own_stable_code() -> None:
    assert InvalidTransition.code == "invalid_transition"
    assert UnauthorizedActor.code == "unauthorized_actor"
    assert GuardRejected.code == "out_of_hours"


@pytest.mark.parametrize("error", [InvalidTransition, UnauthorizedActor, GuardRejected])
def test_every_refusal_is_a_domain_error(error: type[DomainError]) -> None:
    assert issubclass(error, DomainError)


def test_the_detail_names_state_event_and_actor_for_the_logs() -> None:
    with pytest.raises(UnauthorizedActor) as refused:
        transition(State.BLOCKED, Event.OPERATOR_RELEASE, Actor.OPERATOR, hours_open=True)

    assert refused.value.detail == "state=BLOCKED event=operator_release actor=OPERATOR"


def test_the_public_view_never_carries_the_internal_detail() -> None:
    with pytest.raises(GuardRejected) as refused:
        transition(State.AUTO, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, hours_open=False)

    public = refused.value.public()

    assert public["code"] == "out_of_hours"
    assert "AUTO" not in str(public)
    assert "OPERATOR" not in str(public)
