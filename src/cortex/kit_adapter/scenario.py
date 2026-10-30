"""Reads a kit scenario (`examples/T*.json`) into what the harness needs to run it.

The scenario's id, description and `expected` block are dropped on load and never reach the
harness: the adapter cannot answer from a precomputed result, it can only run the scenario. An
unknown key is refused instead of ignored, so a new kind of variant fails loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cortex.config import MAX_CONTROL_VERSION
from cortex.identifiers import EXTERNAL_ID

DROPPED_KEYS = frozenset({"id", "description", "expected"})
# The kit's chat events carry fixtures of their own (`expected_decision` ...): dropped on load,
# so the event reader sees only what an event really holds.
DROPPED_EVENT_PREFIXES = ("expected",)
KNOWN_KEYS = frozenset(
    {
        "tenant_id",
        "principal_id",
        "conversation_id",
        "message",
        "model_script",
        "preconditions",
        "faults",
        "incoming_event",
        "run_id",
        "tools",
        "expected_control_version",
    }
)
# The kit's scenarios carry no user message; the runner needs one.
DEFAULT_MESSAGE = "Hola"


class UnsupportedScenario(Exception):  # noqa: N818 - named after the situation
    """The scenario is malformed or uses something the harness does not handle."""


@dataclass(frozen=True, slots=True)
class Scenario:
    tenant_id: str
    principal_id: str
    conversation_id: str
    message: str
    model_script: tuple[Mapping[str, object], ...]
    preconditions: Mapping[str, object] = field(default_factory=dict)
    faults: Mapping[str, object] = field(default_factory=dict)
    incoming_event: Mapping[str, object] | None = None
    # The id of the main run, when the scenario gives one (the runner accepts a run_id).
    run_id: str | None = None
    # The tools the main run may use (None: the whole catalog), names checked against the catalog
    # once it is loaded.
    tools: frozenset[str] | None = None
    # The control version the caller saw (None: the caller does not check it).
    expected_control_version: int | None = None


def load_scenario(raw: Mapping[str, object]) -> Scenario:
    data = {key: value for key, value in raw.items() if key not in DROPPED_KEYS}
    _check_encodable(data, "the scenario")
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise UnsupportedScenario(f"unknown scenario keys: {sorted(unknown)}")

    script = data.get("model_script")
    if not isinstance(script, list) or not all(isinstance(step, dict) for step in script):
        raise UnsupportedScenario("model_script must be a list of objects")
    incoming = data.get("incoming_event")
    if incoming is not None and not isinstance(incoming, dict):
        raise UnsupportedScenario("incoming_event must be an object")
    return Scenario(
        tenant_id=_text(data, "tenant_id"),
        principal_id=_text(data, "principal_id"),
        conversation_id=conversation_id(_text(data, "conversation_id")),
        message=_text(data, "message") if "message" in data else DEFAULT_MESSAGE,
        model_script=tuple(script),
        preconditions=_object(data, "preconditions"),
        faults=_object(data, "faults"),
        incoming_event=None if incoming is None else _without_fixture_keys(incoming),
        run_id=_run_id(data["run_id"]) if "run_id" in data else None,
        tools=_tools(data["tools"]) if "tools" in data else None,
        expected_control_version=(
            _control_version(data["expected_control_version"])
            if "expected_control_version" in data
            else None
        ),
    )


def _check_encodable(node: object, where: str) -> None:
    """Every string of the scenario, keys included, can be encoded as UTF-8.

    JSON accepts a lone surrogate (`"\\ud800"`), and no UTF-8 encoder does: it would fail deep
    inside the harness instead of here. The reason names where the string is, never the string.
    """
    if isinstance(node, str):
        try:
            node.encode("utf-8")
        except UnicodeEncodeError:
            raise UnsupportedScenario(f"{where} holds a string UTF-8 cannot encode") from None
    elif isinstance(node, Mapping):
        for key, value in node.items():
            _check_encodable(key, f"a key of {where}")
            _check_encodable(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_encodable(item, f"{where}[{index}]")


def _without_fixture_keys(event: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in event.items() if not key.startswith(DROPPED_EVENT_PREFIXES)
    }


def conversation_id(value: str) -> str:
    """A conversation id in the closed alphabet the harness accepts, or an unsupported scenario."""
    if EXTERNAL_ID.fullmatch(value) is None:
        raise UnsupportedScenario(f"not a valid conversation id: {value[:40]!r}")
    return value


def _run_id(value: object) -> str:
    """A run id the scenario chose, held to the same closed alphabet as any external id."""
    if not isinstance(value, str) or EXTERNAL_ID.fullmatch(value) is None:
        raise UnsupportedScenario(f"not a valid run_id: {str(value)[:40]!r}")
    return value


def _tools(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise UnsupportedScenario("tools must be a list of tool names")
    return frozenset(value)


def _control_version(value: object) -> int:
    # `type(...) is int` and not isinstance: a JSON `true` must not pass as the number 1.
    if type(value) is not int or not 0 <= value <= MAX_CONTROL_VERSION:
        raise UnsupportedScenario(
            f"expected_control_version must be an integer between 0 and {MAX_CONTROL_VERSION}"
        )
    return value


def run_id_for(conversation_id: str, turn: int | None = None) -> str:
    """The id of the run the harness derives from a conversation (and, interleaved, a turn).

    Derived in one place so the result is held to the same closed alphabet and length as any
    run id: a conversation id that fits but whose run id does not is an unsupported scenario.
    """
    run_id = f"run-{conversation_id}" if turn is None else f"run-{conversation_id}-{turn}"
    if EXTERNAL_ID.fullmatch(run_id) is None:
        raise UnsupportedScenario(f"the run id derived from {conversation_id[:40]!r} is invalid")
    return run_id


def _text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise UnsupportedScenario(f"{key} must be a non-empty string")
    return value


def _object(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise UnsupportedScenario(f"{key} must be an object")
    return value
