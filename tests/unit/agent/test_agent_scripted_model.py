"""The scripted model is strict: an unplanned call fails the test instead of passing quietly."""

from collections.abc import Sequence

import pytest

from merchant_assistant.agent.adapters.scripted_model import (
    COMPLETION_TOKENS,
    PROMPT_TOKENS,
    SCRIPTED_MODEL_NAME,
    ScriptedChatModel,
    ScriptExhausted,
    calls_reply,
    text_reply,
    tool_call,
)
from merchant_assistant.agent.domain.messages import Message, ModelReply, Role
from merchant_assistant.agent.domain.tools import TOOL_SPECS, ToolSpec
from merchant_assistant.shared.errors import DomainError

HELLO = (Message(Role.USER, "hola"),)
TOOLS = tuple(TOOL_SPECS.values())


def test_steps_are_played_in_order_and_every_call_is_recorded() -> None:
    model = ScriptedChatModel([text_reply("uno"), text_reply("dos")])

    first = model.complete(HELLO, TOOLS)
    second = model.complete(HELLO, TOOLS[:1])

    assert (first.reply.text, second.reply.text) == ("uno", "dos")
    assert [call.tool_names for call in model.calls] == [
        ("buscar_documentacion", "consultar_ventas", "crear_reclamo"),
        ("buscar_documentacion",),
    ]
    assert model.calls[0].messages == HELLO


def test_a_completion_names_the_served_model_and_the_usage() -> None:
    completion = ScriptedChatModel([text_reply("x")]).complete(HELLO, TOOLS)

    assert (completion.model, completion.prompt_tokens, completion.completion_tokens) == (
        SCRIPTED_MODEL_NAME,
        PROMPT_TOKENS,
        COMPLETION_TOKENS,
    )


def test_a_call_beyond_the_script_fails_and_is_not_a_provider_error() -> None:
    model = ScriptedChatModel([text_reply("x")])
    model.complete(HELLO, TOOLS)

    with pytest.raises(ScriptExhausted) as exhausted:
        model.complete(HELLO, TOOLS)

    assert not isinstance(exhausted.value, DomainError)
    assert model.call_count == 2


def test_a_step_can_read_what_the_model_would_see_or_fail_like_a_provider() -> None:
    class ModelDownError(DomainError):
        code = "model_unavailable"

    def echo(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        return text_reply(f"{len(messages)} mensajes, {len(tools)} herramientas")

    def down(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        raise ModelDownError("503")

    model = ScriptedChatModel([echo, down])

    assert model.complete(HELLO, TOOLS).reply.text == "1 mensajes, 3 herramientas"
    with pytest.raises(ModelDownError):
        model.complete(HELLO, TOOLS)


def test_tool_calls_get_unique_ids_and_keep_raw_text_as_given() -> None:
    reply = calls_reply(tool_call("crear_reclamo", {"motivo": "Pago"}), tool_call("x", "not json"))

    first, second = reply.tool_calls
    assert first.id != second.id
    assert first.arguments == '{"motivo": "Pago"}'
    assert second.arguments == "not json"
