"""Why a run ended (brief section 2).

The contract allows exactly five finish reasons. The finer cause travels in `FinishDetail`,
and every detail implies one reason, so the decision table lives in one place: `REASON_OF`.

The rule behind the table: BLOCKED means something was refused for safety; NEEDS_HUMAN means
the next step belongs to a person.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class FinishReason(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    LIMIT_REACHED = "LIMIT_REACHED"
    FAILED = "FAILED"


class FinishDetail(StrEnum):
    ANSWERED = "answered"
    FOLLOWUP_DECIDED = "followup_decided"
    AUTH_INVALID = "auth_invalid"
    CONVERSATION_BUSY = "conversation_busy"
    RUN_ID_CONFLICT = "run_id_conflict"
    CONVERSATION_BLOCKED = "conversation_blocked"
    SCOPE_VIOLATION = "scope_violation"
    HUMAN_IN_CONTROL = "human_in_control"
    OPERATOR_TOOK_OVER = "operator_took_over"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    MAX_STEPS = "max_steps"
    TOTAL_TIMEOUT = "total_timeout"
    WRITE_OUTCOME_UNKNOWN = "write_outcome_unknown"
    MODEL_UNAVAILABLE = "model_unavailable"
    SCRIPT_EXHAUSTED = "script_exhausted"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    INTERNAL_ERROR = "internal_error"
    RUN_INTERRUPTED = "run_interrupted"


REASON_OF: Mapping[FinishDetail, FinishReason] = MappingProxyType(
    {
        FinishDetail.ANSWERED: FinishReason.COMPLETED,
        FinishDetail.FOLLOWUP_DECIDED: FinishReason.COMPLETED,
        FinishDetail.AUTH_INVALID: FinishReason.BLOCKED,
        FinishDetail.CONVERSATION_BUSY: FinishReason.BLOCKED,
        FinishDetail.RUN_ID_CONFLICT: FinishReason.BLOCKED,
        FinishDetail.CONVERSATION_BLOCKED: FinishReason.BLOCKED,
        FinishDetail.SCOPE_VIOLATION: FinishReason.BLOCKED,
        FinishDetail.HUMAN_IN_CONTROL: FinishReason.NEEDS_HUMAN,
        FinishDetail.OPERATOR_TOOK_OVER: FinishReason.NEEDS_HUMAN,
        FinishDetail.AWAITING_CONFIRMATION: FinishReason.NEEDS_HUMAN,
        FinishDetail.MAX_STEPS: FinishReason.LIMIT_REACHED,
        FinishDetail.TOTAL_TIMEOUT: FinishReason.LIMIT_REACHED,
        FinishDetail.WRITE_OUTCOME_UNKNOWN: FinishReason.FAILED,
        FinishDetail.MODEL_UNAVAILABLE: FinishReason.FAILED,
        FinishDetail.SCRIPT_EXHAUSTED: FinishReason.FAILED,
        FinishDetail.INVALID_MODEL_OUTPUT: FinishReason.FAILED,
        FinishDetail.INTERNAL_ERROR: FinishReason.FAILED,
        FinishDetail.RUN_INTERRUPTED: FinishReason.FAILED,
    }
)
