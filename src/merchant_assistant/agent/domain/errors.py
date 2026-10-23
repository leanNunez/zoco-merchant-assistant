"""Why the agent refused a message, a tool call or a gate operation.

Each error has a stable `code` and a safe `user_message`. The `detail` is for the logs only.
Nothing the model or the user typed is ever echoed in `public()`: a tool argument can be
hostile text (an injection attempt) or a secret the user pasted by mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from merchant_assistant.shared.errors import DomainError

# A field name the model invented is untrusted too. It is shown back only when it is a short
# plain identifier, which is enough for the model to know what to drop.
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}", re.ASCII)
REDACTED_FIELD = "<redacted>"
WHOLE_PAYLOAD = "arguments"


class AgentError(DomainError):
    """Base of every refusal raised by the agent domain."""


class InvalidMessage(AgentError):  # noqa: N818 - named after the situation
    """A message or a model reply does not have the shape its role requires."""

    code: ClassVar[str] = "invalid_message"
    user_message: ClassVar[str] = "The message is not well formed."


class ToolNotAvailable(AgentError):  # noqa: N818 - named after the situation
    """The model asked for a tool that is not offered right now (or does not exist)."""

    code: ClassVar[str] = "tool_not_available"
    user_message: ClassVar[str] = "That tool is not available right now."


class Problem(StrEnum):
    """The closed vocabulary of what can be wrong with a tool's arguments."""

    NOT_JSON_OBJECT = "not_json_object"
    DUPLICATE_KEY = "duplicate_key"
    TOO_LARGE = "too_large"
    MISSING = "missing"
    UNEXPECTED_FIELD = "unexpected_field"
    WRONG_TYPE = "wrong_type"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    BAD_FORMAT = "bad_format"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ArgumentIssue:
    """One problem with one field. It names the field, never the value."""

    field: str
    problem: Problem

    def __post_init__(self) -> None:
        if _PLAIN_NAME.fullmatch(self.field) is None:
            object.__setattr__(self, "field", REDACTED_FIELD)


class InvalidToolArguments(AgentError):  # noqa: N818 - named after the situation
    """The arguments break the tool's contract. Rejected, never repaired."""

    code: ClassVar[str] = "invalid_arguments"
    user_message: ClassVar[str] = (
        "The tool arguments do not meet the tool contract. Fix the listed fields and retry."
    )

    def __init__(self, issues: tuple[ArgumentIssue, ...], detail: str = "") -> None:
        super().__init__(detail)
        self.issues = issues

    def public(self) -> dict[str, str]:
        view = super().public()
        view["issues"] = "; ".join(f"{issue.field}: {issue.problem}" for issue in self.issues)
        return view
