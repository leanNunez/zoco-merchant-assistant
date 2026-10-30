"""Harness limits and policies (brief section 2), loaded strictly.

The kit's `harness_config.json` exposes the security policies as flags. A flag is not a switch
here: the harness always enforces them, and a configuration that loosens one (for example
`strict_tenant_scope: false` or `log_secrets: true`) is refused at load time instead of being
obeyed. Unknown keys are refused too, so a typo cannot silently disable anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import ClassVar

from merchant_assistant.shared.errors import DomainError

SCHEMA_VERSION = "2.0"

# The longest text the harness takes in or lets out. 4096 is WhatsApp's limit for one text
# message: a longer user message is refused at admission, before it is stored or the model is
# asked, and a longer final answer is invalid model output (it could not be delivered anyway).
MAX_MESSAGE_CHARS = 4096
MAX_ANSWER_CHARS = 4096

# The largest control version the harness accepts, from a scenario or from an event. Versions are
# counters of takeovers; the bound is the largest 32-bit signed integer, far beyond any real
# conversation, and keeps a hostile value away from the storage layer.
MAX_CONTROL_VERSION = 2**31 - 1

# The only accepted value of each policy: the safe one.
REQUIRED_POLICIES: Mapping[str, bool] = MappingProxyType(
    {
        "strict_tenant_scope": True,
        "writing_requires_exact_confirmation": True,
        "persist_idempotency": True,
        "log_secrets": False,
        "operator_takeover_blocks_output": True,
    }
)

# Inclusive bounds. A limit outside them is a configuration error, not something to clamp.
_BOUNDS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "max_steps": (1, 50),
        "total_timeout_ms": (1, 600_000),
        "tool_timeout_ms": (1, 600_000),
        "max_read_retries": (0, 5),
        "context_window_messages": (1, 200),
    }
)


class ConfigError(DomainError):
    code: ClassVar[str] = "invalid_config"
    user_message: ClassVar[str] = "The harness configuration is invalid."


@dataclass(frozen=True, slots=True)
class RunLimits:
    """How far one run may go: steps (model calls), total time, time per tool attempt."""

    max_steps: int
    total_timeout_ms: int
    tool_timeout_ms: int
    max_read_retries: int
    context_window_messages: int

    def __post_init__(self) -> None:
        # No rule ties tool_timeout_ms to total_timeout_ms: a tool limit above the total is
        # harmless, because each attempt gets min(tool_timeout_ms, the time the run has left)
        # (`Budget.attempt_timeout_ms`). Each limit only has to be inside its own bounds.
        for name, (low, high) in _BOUNDS.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"{name} must be an integer in [{low}, {high}], got {value!r}")

    def with_max_steps(self, max_steps: int) -> RunLimits:
        return self.with_limit("max_steps", max_steps)

    def with_limit(self, name: str, value: int) -> RunLimits:
        return self.with_limits({name: value})

    def with_limits(self, changes: Mapping[str, int]) -> RunLimits:
        """The same limits with some changed, all at once and then validated once, so the order
        in which they are given never matters. Unknown names and out-of-range values are
        refused."""
        unknown = set(changes) - set(_BOUNDS)
        if unknown:
            raise ConfigError(f"unknown limits {sorted(unknown)}")
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    limits: RunLimits
    allowed_finish_reasons: frozenset[str]


def load_harness_config(data: Mapping[str, object]) -> HarnessConfig:
    _expect_keys(data, {"schema_version", "defaults", "policies", "allowed_finish_reasons"}, "")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version {data['schema_version']!r}")

    defaults = _mapping(data["defaults"], "defaults")
    _expect_keys(defaults, set(_BOUNDS), "defaults.")
    limits = RunLimits(
        max_steps=_integer(defaults, "max_steps"),
        total_timeout_ms=_integer(defaults, "total_timeout_ms"),
        tool_timeout_ms=_integer(defaults, "tool_timeout_ms"),
        max_read_retries=_integer(defaults, "max_read_retries"),
        context_window_messages=_integer(defaults, "context_window_messages"),
    )

    policies = _mapping(data["policies"], "policies")
    _expect_keys(policies, set(REQUIRED_POLICIES), "policies.")
    for name, safe in REQUIRED_POLICIES.items():
        if type(policies[name]) is not bool or policies[name] is not safe:
            raise ConfigError(f"policy {name} must be {safe}: the harness never loosens it")

    reasons = data["allowed_finish_reasons"]
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise ConfigError("allowed_finish_reasons must be a list of strings")
    return HarnessConfig(limits=limits, allowed_finish_reasons=frozenset(reasons))


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    # `type(...) is int` and not isinstance: a JSON `true` must not pass as the number 1.
    if type(value) is not int:
        raise ConfigError(f"{key} must be an integer, got {type(value).__name__}")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _expect_keys(data: Mapping[str, object], expected: set[str], prefix: str) -> None:
    missing, unknown = expected - set(data), set(data) - expected
    if missing or unknown:
        raise ConfigError(
            f"configuration keys differ: missing {sorted(prefix + k for k in missing)}, "
            f"unknown {sorted(prefix + k for k in unknown)}"
        )
