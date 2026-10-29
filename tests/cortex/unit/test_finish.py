from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason


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
        FinishDetail.CONVERSATION_BLOCKED,
        FinishDetail.SCOPE_VIOLATION,
    }
    assert human == {
        FinishDetail.HUMAN_IN_CONTROL,
        FinishDetail.OPERATOR_TOOK_OVER,
        FinishDetail.AWAITING_CONFIRMATION,
    }
