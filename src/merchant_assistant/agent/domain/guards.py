"""The guard every tool call crosses before anything runs.

`authorize` decides, deterministically, whether a call the model proposed may proceed. The
checks run in a fixed order and the first failure wins:

1. The name is an exact key of the tools offered right now. Unknown names, different
   casing, stray spaces and tools the conversation state does not offer are all
   `ToolNotAvailable`: the model cannot tell them apart, and does not need to.
2. The raw arguments are at most `MAX_ARGUMENTS_CHARS` characters.
3. They parse as one JSON object with no repeated key. A repeated key is refused instead of
   letting the last one win silently, so the value checked is the value the model meant.
4. The tool's argument model validates them in strict mode.

Passing the guard authorizes a READ tool to run. For an EFFECT tool it only allows a pending
action to be created (see `pending.py`); nothing is executed from a model's call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from merchant_assistant.agent.domain.errors import (
    WHOLE_PAYLOAD,
    ArgumentIssue,
    InvalidToolArguments,
    Problem,
    ToolNotAvailable,
)
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.tools import ToolSpec

MAX_ARGUMENTS_CHARS = 4000

# Pydantic error types mapped to the closed vocabulary. Anything else is `invalid`.
_PROBLEM_BY_ERROR_TYPE: Mapping[str, Problem] = {
    "missing": Problem.MISSING,
    "extra_forbidden": Problem.UNEXPECTED_FIELD,
    "string_type": Problem.WRONG_TYPE,
    "string_sub_type": Problem.WRONG_TYPE,
    "string_unicode": Problem.WRONG_TYPE,
    "model_type": Problem.WRONG_TYPE,
    "model_attributes_type": Problem.WRONG_TYPE,
    "dict_type": Problem.WRONG_TYPE,
    "string_too_short": Problem.TOO_SHORT,
    "too_short": Problem.TOO_SHORT,
    "string_too_long": Problem.TOO_LONG,
    "too_long": Problem.TOO_LONG,
    "string_pattern_mismatch": Problem.BAD_FORMAT,
}


@dataclass(frozen=True, slots=True)
class ValidatedCall:
    """A call that passed every guard: the arguments are the validated, normalized model."""

    call_id: str
    spec: ToolSpec
    arguments: BaseModel


class _DuplicateKey(Exception):  # noqa: N818 - private signal, never leaves this module
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def authorize(call: ToolCall, available: Mapping[str, ToolSpec]) -> ValidatedCall:
    spec = available.get(call.name)
    if spec is None:
        raise ToolNotAvailable(f"tool not offered: {_preview(call.name)}")

    raw = call.arguments
    if len(raw) > MAX_ARGUMENTS_CHARS:
        raise _refuse(spec, (ArgumentIssue(WHOLE_PAYLOAD, Problem.TOO_LARGE),), raw)

    try:
        parsed: object = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as duplicate:
        raise _refuse(spec, (ArgumentIssue(duplicate.key, Problem.DUPLICATE_KEY),), raw) from None
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON; RecursionError a payload nested too deep.
        raise _refuse(spec, (ArgumentIssue(WHOLE_PAYLOAD, Problem.NOT_JSON_OBJECT),), raw) from None
    if not isinstance(parsed, dict):
        raise _refuse(spec, (ArgumentIssue(WHOLE_PAYLOAD, Problem.NOT_JSON_OBJECT),), raw)

    try:
        arguments = spec.args_model.model_validate(parsed, strict=True)
    except ValidationError as invalid:
        raise _refuse(spec, _issues(invalid), raw) from None
    return ValidatedCall(call_id=call.id, spec=spec, arguments=arguments)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _issues(invalid: ValidationError) -> tuple[ArgumentIssue, ...]:
    issues: list[ArgumentIssue] = []
    for error in invalid.errors():
        field = ".".join(str(part) for part in error["loc"]) or WHOLE_PAYLOAD
        issue = ArgumentIssue(field, _PROBLEM_BY_ERROR_TYPE.get(error["type"], Problem.INVALID))
        if issue not in issues:
            issues.append(issue)
    return tuple(issues)


def _refuse(spec: ToolSpec, issues: tuple[ArgumentIssue, ...], raw: str) -> InvalidToolArguments:
    # The detail names the problems and the size, never the payload: logs are not a safe
    # place for text that may be hostile or carry a secret.
    summary = ", ".join(f"{issue.field}={issue.problem}" for issue in issues)
    return InvalidToolArguments(issues, f"tool={spec.name} issues=[{summary}] chars={len(raw)}")


def _preview(text: str, limit: int = 40) -> str:
    return repr(text[:limit] + "..." if len(text) > limit else text)
