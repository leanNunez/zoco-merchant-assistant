import copy
import json
from pathlib import Path
from typing import Any

import pytest

from cortex.config import REQUIRED_POLICIES, ConfigError, RunLimits, load_harness_config

KIT = Path(__file__).resolve().parents[3] / "kit"
KIT_CONFIG: dict[str, Any] = json.loads((KIT / "harness_config.json").read_text(encoding="utf-8"))


def _with(path: tuple[str, ...], value: object) -> dict[str, Any]:
    data = copy.deepcopy(KIT_CONFIG)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def test_the_kit_configuration_loads_with_its_limits() -> None:
    config = load_harness_config(KIT_CONFIG)

    assert config.limits == RunLimits(
        max_steps=6,
        total_timeout_ms=15000,
        tool_timeout_ms=3000,
        max_read_retries=2,
        context_window_messages=12,
    )


@pytest.mark.parametrize("policy", sorted(REQUIRED_POLICIES))
def test_loosening_any_policy_is_refused(policy: str) -> None:
    loosened = _with(("policies", policy), not REQUIRED_POLICIES[policy])

    with pytest.raises(ConfigError, match=f"policy {policy}"):
        load_harness_config(loosened)


def test_a_policy_must_be_a_real_boolean() -> None:
    with pytest.raises(ConfigError, match="strict_tenant_scope"):
        load_harness_config(_with(("policies", "strict_tenant_scope"), 1))


@pytest.mark.parametrize(
    "path",
    [("policies", "skip_checks"), ("defaults", "max_stepz"), ("debug",)],
)
def test_an_unknown_key_is_refused_instead_of_ignored(path: tuple[str, ...]) -> None:
    with pytest.raises(ConfigError, match="unknown"):
        load_harness_config(_with(path, True))


@pytest.mark.parametrize(
    ("limit", "value"),
    [("max_steps", 0), ("max_steps", 51), ("max_read_retries", -1), ("max_steps", True)],
)
def test_limits_outside_their_bounds_are_refused(limit: str, value: object) -> None:
    with pytest.raises(ConfigError, match=limit):
        load_harness_config(_with(("defaults", limit), value))


def test_a_tool_limit_above_the_total_is_accepted_because_each_attempt_is_clamped() -> None:
    # Budget.attempt_timeout_ms gives an attempt min(tool_timeout_ms, the time left).
    config = load_harness_config(_with(("defaults", "tool_timeout_ms"), 20000))

    assert (config.limits.tool_timeout_ms, config.limits.total_timeout_ms) == (20000, 15000)
