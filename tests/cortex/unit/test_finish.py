import pytest

from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason, missing_failure_event


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
    ("reason", "emitted", "missing"),
    [
        (FinishReason.BLOCKED, ["run_started"], "tool_rejected"),
        (FinishReason.FAILED, ["model_failed"], "tool_error"),
        (FinishReason.BLOCKED, ["tool_rejected"], None),
        (FinishReason.FAILED, ["tool_rejected"], None),
        (FinishReason.FAILED, ["tool_error"], None),
        (FinishReason.COMPLETED, [], None),
        (FinishReason.NEEDS_HUMAN, [], None),
        (FinishReason.LIMIT_REACHED, [], None),
    ],
)
def test_a_blocked_or_failed_run_without_a_tool_event_gets_exactly_one(
    reason: FinishReason, emitted: list[str], missing: str | None
) -> None:
    assert missing_failure_event(reason, emitted) == missing
