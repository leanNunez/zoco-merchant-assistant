"""Reads a kit scenario (`examples/T*.json`) into what the harness needs to run it.

The scenario's id, description and `expected` block are dropped on load and never reach the
harness: the adapter cannot answer from a precomputed result, it can only run the scenario. An
unknown key is refused instead of ignored, so a new kind of variant fails loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cortex.memory.tenant_store import EXTERNAL_ID

DROPPED_KEYS = frozenset({"id", "description", "expected"})
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


def load_scenario(raw: Mapping[str, object]) -> Scenario:
    data = {key: value for key, value in raw.items() if key not in DROPPED_KEYS}
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
        incoming_event=incoming,
        run_id=_run_id(data["run_id"]) if "run_id" in data else None,
    )


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
