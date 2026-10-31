import json

import pytest

from cortex.runner.model import FinalAnswer, InvalidModelOutput, Prompt, ScriptExhausted
from cortex.runner.scripted_model import ScriptedModel
from merchant_assistant.agent.domain.messages import ToolCall

PROMPT = Prompt(messages=(), state={}, tools=())


def test_it_plays_tool_requests_and_answers_in_order() -> None:
    model = ScriptedModel(
        [{"tool": "consultar_ventas", "args": {"fecha": "2026-10-15"}}, {"final": "15 ventas"}]
    )

    first = model.next_turn(PROMPT, timeout_ms=1000)
    second = model.next_turn(PROMPT, timeout_ms=1000)

    assert first == ToolCall("call-1", "consultar_ventas", json.dumps({"fecha": "2026-10-15"}))
    assert second == FinalAnswer("15 ventas")
    assert model.prompts == [PROMPT, PROMPT]


def test_running_out_of_script_is_an_explicit_failure() -> None:
    model = ScriptedModel([])

    with pytest.raises(ScriptExhausted):
        model.next_turn(PROMPT, timeout_ms=1000)


@pytest.mark.parametrize(
    "step",
    [{"final": "x", "tool": "y", "args": {}}, {"tool": "y"}, {"final": 3}, {}],
)
def test_a_malformed_step_is_a_malformed_answer(step: dict[str, object]) -> None:
    with pytest.raises(InvalidModelOutput):
        ScriptedModel([step]).next_turn(PROMPT, timeout_ms=1000)


def test_two_models_never_share_their_call_numbers() -> None:
    script = [{"tool": "consultar_ventas", "args": {}}]

    first = ScriptedModel(script).next_turn(PROMPT, timeout_ms=1000)
    second = ScriptedModel(script).next_turn(PROMPT, timeout_ms=1000)

    assert isinstance(first, ToolCall)
    assert isinstance(second, ToolCall)
    assert first.id == second.id == "call-1"
