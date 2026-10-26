import pytest

from merchant_assistant.agent.domain.errors import AgentError, InvalidMessage
from merchant_assistant.agent.domain.messages import Author, Message, ModelReply, Role, ToolCall

CALL = ToolCall(id="call_1", name="consultar_ventas", arguments='{"fecha": "2026-10-03"}')


def test_a_tool_call_keeps_the_raw_arguments_exactly_as_the_model_wrote_them() -> None:
    raw = '{ "fecha" :"2026-10-03" ,"fecha":"x"}'

    assert ToolCall(id="c", name="consultar_ventas", arguments=raw).arguments == raw


def test_a_tool_call_without_an_id_is_refused() -> None:
    with pytest.raises(InvalidMessage):
        ToolCall(id="  ", name="consultar_ventas", arguments="{}")


@pytest.mark.parametrize(
    ("role", "content", "tool_calls", "tool_call_id"),
    [
        pytest.param(Role.SYSTEM, "rules", (), None, id="system with content"),
        pytest.param(Role.USER, "hola", (), None, id="user with content"),
        pytest.param(Role.USER, "", (), None, id="user with empty content"),
        pytest.param(Role.ASSISTANT, "hola", (), None, id="assistant with text"),
        pytest.param(Role.ASSISTANT, None, (CALL,), None, id="assistant with a call"),
        pytest.param(Role.ASSISTANT, "ok", (CALL,), None, id="assistant with both"),
        pytest.param(Role.TOOL, '{"ok": true}', (), "call_1", id="tool result"),
    ],
)
def test_well_formed_messages_are_accepted(
    role: Role, content: str | None, tool_calls: tuple[ToolCall, ...], tool_call_id: str | None
) -> None:
    message = Message(role, content, tool_calls, tool_call_id)

    assert (message.role, message.tool_calls, message.tool_call_id) == (
        role,
        tool_calls,
        tool_call_id,
    )


@pytest.mark.parametrize(
    ("role", "content", "tool_calls", "tool_call_id"),
    [
        pytest.param(Role.SYSTEM, None, (), None, id="system without content"),
        pytest.param(Role.SYSTEM, "x", (CALL,), None, id="system with tool calls"),
        pytest.param(Role.SYSTEM, "x", (), "call_1", id="system answering a call"),
        pytest.param(Role.USER, None, (), None, id="user without content"),
        pytest.param(Role.USER, "x", (CALL,), None, id="user with tool calls"),
        pytest.param(Role.USER, "x", (), "call_1", id="user answering a call"),
        pytest.param(Role.ASSISTANT, None, (), None, id="assistant with nothing"),
        pytest.param(Role.ASSISTANT, "x", (), "call_1", id="assistant answering a call"),
        pytest.param(Role.ASSISTANT, None, (CALL, CALL), None, id="assistant repeating a call id"),
        pytest.param(Role.TOOL, "x", (), None, id="tool without the call it answers"),
        pytest.param(Role.TOOL, "x", (), " ", id="tool with a blank call id"),
        pytest.param(Role.TOOL, None, (), "call_1", id="tool without a result"),
        pytest.param(Role.TOOL, "x", (CALL,), "call_1", id="tool with tool calls"),
    ],
)
def test_a_message_that_breaks_the_rules_of_its_role_is_refused(
    role: Role, content: str | None, tool_calls: tuple[ToolCall, ...], tool_call_id: str | None
) -> None:
    with pytest.raises(InvalidMessage):
        Message(role, content, tool_calls, tool_call_id)


def test_a_model_reply_may_carry_text_tool_calls_or_both() -> None:
    assert ModelReply("hola", ()).text == "hola"
    assert ModelReply(None, (CALL,)).tool_calls == (CALL,)
    assert ModelReply("un momento", (CALL,)).tool_calls == (CALL,)


@pytest.mark.parametrize("text", [None, "", "   \n"])
def test_a_model_reply_with_neither_text_nor_tool_calls_is_refused(text: str | None) -> None:
    with pytest.raises(InvalidMessage):
        ModelReply(text, ())


def test_a_model_reply_cannot_repeat_a_tool_call_id() -> None:
    with pytest.raises(InvalidMessage):
        ModelReply(None, (CALL, CALL))


def test_message_errors_belong_to_the_agent_family() -> None:
    assert issubclass(InvalidMessage, AgentError)
    assert InvalidMessage.code == "invalid_message"


def test_an_operator_message_is_an_assistant_message_marked_with_its_author() -> None:
    message = Message(Role.ASSISTANT, "Hola, soy Ana del equipo.", author=Author.OPERATOR)

    assert message.author is Author.OPERATOR
    assert message != Message(Role.ASSISTANT, "Hola, soy Ana del equipo.")
    assert Message(Role.ASSISTANT, "Hola.").author is Author.AGENT


@pytest.mark.parametrize(
    ("role", "content", "tool_calls", "tool_call_id"),
    [
        pytest.param(Role.USER, "hola", (), None, id="as the customer"),
        pytest.param(Role.SYSTEM, "rules", (), None, id="as the system prompt"),
        pytest.param(Role.TOOL, "{}", (), "call_1", id="as a tool result"),
        pytest.param(Role.ASSISTANT, None, (CALL,), None, id="with a tool call"),
        pytest.param(Role.ASSISTANT, "ok", (CALL,), None, id="with text and a tool call"),
        pytest.param(Role.ASSISTANT, "  ", (), None, id="blank"),
    ],
)
@pytest.mark.parametrize("author", [Author.OPERATOR, Author.GATE])
def test_an_operator_or_the_gate_writes_only_plain_text_to_the_customer(
    role: Role,
    content: str | None,
    tool_calls: tuple[ToolCall, ...],
    tool_call_id: str | None,
    author: Author,
) -> None:
    with pytest.raises(InvalidMessage):
        Message(role, content, tool_calls, tool_call_id, author=author)
