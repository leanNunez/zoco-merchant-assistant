import pytest

from cortex.runner.finish import (
    REASON_OF,
    FinishDetail,
    FinishReason,
    explaining_reasons,
    missing_failure_event,
)


def test_every_detail_implies_exactly_one_of_the_five_reasons() -> None:
    assert set(REASON_OF) == set(FinishDetail)
    assert set(REASON_OF.values()) == set(FinishReason)
    assert {reason.value for reason in FinishReason} == {
        "COMPLETED",
        "BLOCKED",
        "NEEDS_HUMAN",
        "LIMIT_REACHED",
        "FAILED",
    }


def test_refusals_for_safety_block_and_hand_offs_need_a_human() -> None:
    blocked = {detail for detail, reason in REASON_OF.items() if reason is FinishReason.BLOCKED}
    human = {detail for detail, reason in REASON_OF.items() if reason is FinishReason.NEEDS_HUMAN}

    assert blocked == {
        FinishDetail.AUTH_INVALID,
        FinishDetail.CONVERSATION_BUSY,
        FinishDetail.RUN_ID_CONFLICT,
        FinishDetail.CONVERSATION_BLOCKED,
        FinishDetail.STALE_VERSION,
        FinishDetail.SCOPE_VIOLATION,
        FinishDetail.MESSAGE_TOO_LONG,
    }
    assert human == {
        FinishDetail.HUMAN_IN_CONTROL,
        FinishDetail.OPERATOR_TOOK_OVER,
        FinishDetail.AWAITING_CONFIRMATION,
    }


@pytest.mark.parametrize(
    ("detail", "emitted", "missing"),
    [
        (FinishDetail.CONVERSATION_BLOCKED, [("run_started", None)], "tool_rejected"),
        (FinishDetail.SCRIPT_EXHAUSTED, [("model_failed", None)], "tool_error"),
        (FinishDetail.SCOPE_VIOLATION, [("tool_rejected", "scope_violation")], None),
        (FinishDetail.INTERNAL_ERROR, [("tool_error", "internal_error")], None),
        (FinishDetail.WRITE_OUTCOME_UNKNOWN, [("tool_error", "outcome_unknown")], None),
        (FinishDetail.ANSWERED, [], None),
        (FinishDetail.AWAITING_CONFIRMATION, [], None),
        (FinishDetail.MAX_STEPS, [], None),
    ],
)
def test_a_blocked_or_failed_run_without_a_tool_event_gets_exactly_one(
    detail: FinishDetail, emitted: list[tuple[str, str | None]], missing: str | None
) -> None:
    assert missing_failure_event(detail, emitted) == missing


@pytest.mark.parametrize(
    ("detail", "emitted", "missing"),
    [
        # A read that failed and was retried does not say why the model then failed.
        (FinishDetail.SCRIPT_EXHAUSTED, [("tool_error", "timeout")], "tool_error"),
        (FinishDetail.MODEL_UNAVAILABLE, [("tool_rejected", "invalid_arguments")], "tool_error"),
        # A rejected tool the run went on after does not say why the conversation was refused.
        (FinishDetail.SCOPE_VIOLATION, [("tool_rejected", "tool_not_allowed")], "tool_rejected"),
        # The reason must come with a failure event, not with any event.
        (FinishDetail.SCRIPT_EXHAUSTED, [("model_failed", "script_exhausted")], "tool_error"),
    ],
)
def test_an_unrelated_failure_event_does_not_explain_the_ending(
    detail: FinishDetail, emitted: list[tuple[str, str | None]], missing: str
) -> None:
    assert missing_failure_event(detail, emitted) == missing


def test_every_explained_detail_ends_the_run_blocked_or_failed() -> None:
    for detail in FinishDetail:
        reasons = explaining_reasons(detail)
        assert detail.value in reasons
        if reasons != {detail.value}:
            assert REASON_OF[detail] in (FinishReason.BLOCKED, FinishReason.FAILED)
