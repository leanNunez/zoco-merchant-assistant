"""How a tool call can fail (brief section 5), and what each kind allows.

- TRANSIENT: nothing happened and trying again may work (a read that timed out). Only reads are
  retried, with a cap and inside the run's time.
- PERMANENT: nothing happened and trying again will not help (an invalid request). Never retried.
- UNCERTAIN: the effect may or may not have happened (a write that timed out after the request
  left). Never retried blindly: the operation is looked up by its id first.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from merchant_assistant.shared.errors import DomainError


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNCERTAIN = "uncertain"


class ToolFailure(DomainError):  # noqa: N818 - the base of the three failure kinds
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class TransientFailure(ToolFailure):
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT
    code: ClassVar[str] = "tool_unavailable"
    user_message: ClassVar[str] = "The tool did not answer in time."


class PermanentFailure(ToolFailure):
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT
    code: ClassVar[str] = "tool_refused"
    user_message: ClassVar[str] = "The tool refused the request."


class UncertainOutcome(ToolFailure):
    kind: ClassVar[FailureKind] = FailureKind.UNCERTAIN
    code: ClassVar[str] = "outcome_unknown"
    user_message: ClassVar[str] = "It is not known yet whether the operation was applied."
