"""The kit's preconditions and faults, read before anything runs.

One handler per key. An unknown key, or a known key with a malformed value, stops the scenario
(exit code 2) instead of being ignored: a precondition the harness did not apply would make a
PASS meaningless.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from cortex.config import ConfigError, RunLimits
from cortex.kit_adapter.scenario import UnsupportedScenario
from cortex.tools.adapters.faults import FaultKind
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO
from merchant_assistant.control.states import State

# The kit names the pending action by its kind ("reclamo"); the harness, by its tool.
PENDING_KINDS: Mapping[str, str] = MappingProxyType(
    {"reclamo": CREAR_RECLAMO, CREAR_RECLAMO: CREAR_RECLAMO}
)


@dataclass(frozen=True, slots=True)
class InterleavedMessage:
    tenant_id: str
    conversation_id: str
    message: str


@dataclass(frozen=True, slots=True)
class Setup:
    """What the preconditions ask for, before anything runs."""

    limits: RunLimits
    interleaved: tuple[InterleavedMessage, ...] = field(default_factory=tuple)
    # Writes the user already confirmed before the scenario starts: (tool, arguments).
    confirmed_actions: tuple[tuple[str, Mapping[str, object]], ...] = field(default_factory=tuple)
    # Who controls the conversation when the scenario starts, as Zoco Chat reports it.
    mode: State | None = None
    control_version: int | None = None
    # Writes waiting from before, known only by their kind and id: (tool, operation_id).
    pending: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    restart: bool = False


def read_preconditions(preconditions: Mapping[str, object], limits: RunLimits) -> Setup:
    setup = Setup(limits)
    for key, value in preconditions.items():
        handler = _HANDLERS.get(key)
        if handler is None:
            raise UnsupportedScenario(f"precondition {key!r} is not supported")
        setup = handler(setup, value)
    return setup


def read_faults(
    raw: Mapping[str, object], tools: tuple[str, ...]
) -> dict[str, tuple[FaultKind, ...]]:
    """The kit's `faults`: for each tool, what each of its calls does."""
    faults: dict[str, tuple[FaultKind, ...]] = {}
    for tool, kinds in raw.items():
        if tool not in tools:
            raise UnsupportedScenario(f"faults name an unknown tool: {tool!r}")
        if not isinstance(kinds, list) or not kinds:
            raise UnsupportedScenario(f"the faults of {tool} must be a non-empty list")
        try:
            faults[tool] = tuple(FaultKind(str(kind)) for kind in kinds)
        except ValueError:
            raise UnsupportedScenario(f"unknown fault kind in {tool}: {kinds!r}") from None
    return faults


def _limit(name: str) -> Callable[[Setup, object], Setup]:
    """A precondition that changes one of the run's limits for this scenario."""

    def handler(setup: Setup, value: object) -> Setup:
        if type(value) is not int:
            raise UnsupportedScenario(f"{name} must be an integer")
        try:
            return replace(setup, limits=setup.limits.with_limit(name, value))
        except ConfigError as invalid:
            raise UnsupportedScenario(invalid.detail) from None

    return handler


def _mode(setup: Setup, value: object) -> Setup:
    try:
        mode = State(str(value))
    except ValueError:
        raise UnsupportedScenario(f"unknown conversation state {value!r}") from None
    if setup.mode is not None and setup.mode is not mode:
        raise UnsupportedScenario("current_state and initial_state disagree")
    return replace(setup, mode=mode)


def _version(setup: Setup, value: object) -> Setup:
    if type(value) is not int or value < 0:
        raise UnsupportedScenario("current_version must be a non-negative integer")
    return replace(setup, control_version=value)


def _pending_action(setup: Setup, value: object) -> Setup:
    if not isinstance(value, dict) or set(value) != {"type", "operation_id"}:
        raise UnsupportedScenario("pending_action needs exactly type and operation_id")
    tool = PENDING_KINDS.get(str(value["type"]))
    if tool is None or not isinstance(value["operation_id"], str):
        raise UnsupportedScenario(f"unknown pending action {value!r}")
    return replace(setup, pending=(*setup.pending, (tool, value["operation_id"])))


def _restart(setup: Setup, value: object) -> Setup:
    if type(value) is not bool:
        raise UnsupportedScenario("restart_between_events must be true or false")
    return replace(setup, restart=value)


def _interleaved_conversations(setup: Setup, value: object) -> Setup:
    if not isinstance(value, list) or not value:
        raise UnsupportedScenario("interleaved_conversations must be a non-empty list")
    entries = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"tenant_id", "conversation_id", "message"}:
            raise UnsupportedScenario(
                "each interleaved conversation needs exactly tenant_id, conversation_id and message"
            )
        if not all(isinstance(text, str) and text for text in item.values()):
            raise UnsupportedScenario("interleaved conversation fields must be non-empty strings")
        entries.append(InterleavedMessage(**item))
    return replace(setup, interleaved=tuple(entries))


def _confirmed_actions(setup: Setup, value: object) -> Setup:
    if not isinstance(value, list):
        raise UnsupportedScenario("confirmed_actions must be a list")
    actions = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"tool", "args"}:
            raise UnsupportedScenario("each confirmed action needs exactly tool and args")
        if not isinstance(item["tool"], str) or not isinstance(item["args"], dict):
            raise UnsupportedScenario("a confirmed action is a tool name and an args object")
        actions.append((item["tool"], item["args"]))
    return replace(setup, confirmed_actions=tuple(actions))


_HANDLERS: Mapping[str, Callable[[Setup, object], Setup]] = MappingProxyType(
    {
        "max_steps": _limit("max_steps"),
        "total_timeout_ms": _limit("total_timeout_ms"),
        "tool_timeout_ms": _limit("tool_timeout_ms"),
        "max_read_retries": _limit("max_read_retries"),
        "context_window_messages": _limit("context_window_messages"),
        "interleaved_conversations": _interleaved_conversations,
        "confirmed_actions": _confirmed_actions,
        "current_state": _mode,
        "initial_state": _mode,
        "current_version": _version,
        "pending_action": _pending_action,
        "restart_between_events": _restart,
    }
)
