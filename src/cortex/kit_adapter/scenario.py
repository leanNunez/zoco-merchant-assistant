"""Reads a kit scenario (`examples/T*.json`) into what the harness needs to run it.

The scenario's id, description and `expected` block are dropped on load and never reach the
harness: the adapter cannot answer from a precomputed result, it can only run the scenario. An
unknown key is refused instead of ignored, so a new kind of variant fails loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

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
        conversation_id=_text(data, "conversation_id"),
        message=_text(data, "message") if "message" in data else DEFAULT_MESSAGE,
        model_script=tuple(script),
        preconditions=_object(data, "preconditions"),
        faults=_object(data, "faults"),
        incoming_event=incoming,
    )


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
