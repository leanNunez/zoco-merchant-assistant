"""The kit's preconditions: limits applied together, and every name a fact goes by."""

import pytest

from cortex.config import MAX_CONTROL_VERSION, RunLimits
from cortex.kit_adapter.preconditions import read_faults, read_preconditions
from cortex.kit_adapter.scenario import UnsupportedScenario
from cortex.tools.adapters.faults import FaultKind
from merchant_assistant.control.states import State

DEFAULTS = RunLimits(
    max_steps=6,
    total_timeout_ms=15000,
    tool_timeout_ms=3000,
    max_read_retries=2,
    context_window_messages=12,
)


# --------------------------------------------------------------------------- limits


def test_a_total_time_below_the_default_tool_time_is_accepted() -> None:
    limits = read_preconditions({"total_timeout_ms": 1000}, DEFAULTS).limits

    # Allowed: each attempt gets min(tool_timeout_ms, what the run has left).
    assert (limits.total_timeout_ms, limits.tool_timeout_ms) == (1000, 3000)


@pytest.mark.parametrize(
    "preconditions",
    [
        {"total_timeout_ms": 1000, "tool_timeout_ms": 500},
        {"tool_timeout_ms": 500, "total_timeout_ms": 1000},
    ],
)
def test_the_order_of_the_limit_keys_does_not_matter(preconditions: dict[str, int]) -> None:
    limits = read_preconditions(preconditions, DEFAULTS).limits

    assert (limits.total_timeout_ms, limits.tool_timeout_ms) == (1000, 500)


@pytest.mark.parametrize(
    "preconditions",
    [
        {"total_timeout_ms": 0},
        {"tool_timeout_ms": 600_001},
        {"max_steps": 51},
        {"max_read_retries": -1},
        {"total_timeout_ms": 1000, "max_steps": 0},
        {"max_steps": True},
        {"max_steps": "3"},
    ],
)
def test_a_limit_out_of_range_or_of_the_wrong_type_is_refused(
    preconditions: dict[str, object],
) -> None:
    with pytest.raises(UnsupportedScenario):
        read_preconditions(preconditions, DEFAULTS)


# --------------------------------------------------------------------------- one fact, many names


@pytest.mark.parametrize("name", ["current_state", "initial_state", "conversation_mode"])
def test_every_name_of_the_conversation_mode_is_understood(name: str) -> None:
    assert read_preconditions({name: "HUMAN"}, DEFAULTS).mode is State.HUMAN


@pytest.mark.parametrize("name", ["current_version", "control_version"])
def test_every_name_of_the_control_version_is_understood(name: str) -> None:
    assert read_preconditions({name: 3}, DEFAULTS).control_version == 3


def test_two_names_of_a_fact_that_agree_are_accepted() -> None:
    setup = read_preconditions(
        {
            "conversation_mode": "AUTO",
            "current_state": "AUTO",
            "control_version": 1,
            "current_version": 1,
        },
        DEFAULTS,
    )

    assert (setup.mode, setup.control_version) == (State.AUTO, 1)


@pytest.mark.parametrize(
    "preconditions",
    [
        {"conversation_mode": "AUTO", "current_state": "HUMAN"},
        {"initial_state": "HUMAN", "conversation_mode": "BLOCKED"},
        {"control_version": 1, "current_version": 2},
        {"current_version": 2, "control_version": 1},
    ],
)
def test_two_names_of_a_fact_that_disagree_are_refused(preconditions: dict[str, object]) -> None:
    with pytest.raises(UnsupportedScenario, match="disagree"):
        read_preconditions(preconditions, DEFAULTS)


def test_an_interleaved_conversation_may_name_its_principal() -> None:
    entry = {
        "tenant_id": "comercio_a",
        "principal_id": "operador_a",
        "conversation_id": "conv_a",
        "message": "Hola",
    }

    (read,) = read_preconditions({"interleaved_conversations": [entry]}, DEFAULTS).interleaved

    assert read.principal_id == "operador_a"


def test_an_interleaved_conversation_with_an_unknown_key_is_refused() -> None:
    entry = {"tenant_id": "comercio_a", "conversation_id": "c", "message": "Hola", "role": "admin"}

    with pytest.raises(UnsupportedScenario):
        read_preconditions({"interleaved_conversations": [entry]}, DEFAULTS)


# --------------------------------------------------------------------------- control versions


@pytest.mark.parametrize("name", ["current_version", "control_version"])
def test_a_control_version_up_to_the_bound_is_accepted(name: str) -> None:
    setup = read_preconditions({name: MAX_CONTROL_VERSION}, DEFAULTS)

    assert setup.control_version == MAX_CONTROL_VERSION


@pytest.mark.parametrize("name", ["current_version", "control_version"])
@pytest.mark.parametrize("version", [MAX_CONTROL_VERSION + 1, 2**70, -1])
def test_a_control_version_outside_the_bounds_is_an_unsupported_scenario(
    name: str, version: int
) -> None:
    with pytest.raises(UnsupportedScenario, match="version"):
        read_preconditions({name: version}, DEFAULTS)


# --------------------------------------------------------------------------- letter case


@pytest.mark.parametrize("name", ["initial_state", "current_state", "conversation_mode"])
@pytest.mark.parametrize(("raw", "mode"), [("human", State.HUMAN), ("Blocked", State.BLOCKED)])
def test_the_conversation_mode_is_read_in_any_letter_case(name: str, raw: str, mode: State) -> None:
    assert read_preconditions({name: raw}, DEFAULTS).mode is mode


@pytest.mark.parametrize("kind", ["Reclamo", "RECLAMO", "Crear_Reclamo"])
def test_the_kind_of_a_pending_action_is_read_in_any_letter_case(kind: str) -> None:
    setup = read_preconditions({"pending_action": {"type": kind, "operation_id": "op_1"}}, DEFAULTS)

    assert setup.pending == (("crear_reclamo", "op_1"),)


def test_a_fault_kind_is_read_in_any_letter_case() -> None:
    faults = read_faults({"consultar_ventas": ["timeout", "Success"]}, ("consultar_ventas",))

    assert faults == {"consultar_ventas": (FaultKind.TIMEOUT, FaultKind.SUCCESS)}
