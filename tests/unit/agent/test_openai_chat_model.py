"""The chat adapter against a hand-written client shaped like the SDK's. No network."""

import json
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
import pytest

from merchant_assistant.agent.adapters.errors import (
    ModelAuthError,
    ModelError,
    ModelIncomplete,
    ModelNotFound,
    ModelRefused,
    ModelRequestRejected,
    ModelUnavailable,
)
from merchant_assistant.agent.adapters.openai_chat_model import (
    PROFILES,
    OpenAIChatModel,
    ProviderProfile,
)
from merchant_assistant.agent.domain.messages import Author, Message, Role, ToolCall
from merchant_assistant.agent.domain.notes import Withdrawal, note_text
from merchant_assistant.agent.domain.tools import TOOL_SPECS, ToolSpec
from merchant_assistant.shared.errors import DomainError

REQUEST = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")
FAKE_KEY = "sk-FAKE-do-not-leak-1234567890"
ALL_TOOLS = tuple(TOOL_SPECS.values())


# --------------------------------------------------------------------------- SDK-shaped fakes


@dataclass(frozen=True)
class Function:
    name: str
    arguments: str


@dataclass(frozen=True)
class WireCall:
    id: str
    function: Function
    type: str = "function"


@dataclass(frozen=True)
class CustomCall:
    """The SDK also types a custom tool call, which has no `function`."""

    id: str = "c1"
    type: str = "custom"


@dataclass(frozen=True)
class WireMessage:
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[Any] | None = None
    role: str = "assistant"


@dataclass(frozen=True)
class WireChoice:
    message: WireMessage
    finish_reason: str = "stop"


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class WireCompletion:
    choices: list[WireChoice]
    model: str = "gpt-4o-mini-2024-07-18"
    usage: Usage | None = field(default_factory=lambda: Usage(11, 7))


def answer(text: str = "Hola.", **overrides: Any) -> WireCompletion:
    return WireCompletion([WireChoice(WireMessage(content=text))], **overrides)


@dataclass
class FakeCompletions:
    response: WireCompletion | None = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, *args: Any, **kwargs: Any) -> WireCompletion:
        assert not args
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response if self.response is not None else answer()


@dataclass
class FakeChat:
    completions: FakeCompletions


@dataclass
class FakeClient:
    chat: FakeChat

    @property
    def sent(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


def replying(response: WireCompletion | None = None, error: Exception | None = None) -> FakeClient:
    return FakeClient(FakeChat(FakeCompletions(response, error)))


def adapter(
    client: FakeClient,
    profile: str = "openai",
    *,
    strict: bool = False,
    model: str = "gpt-4o-mini",
) -> OpenAIChatModel:
    return OpenAIChatModel(
        client,
        model,
        profile=PROFILES[profile],
        temperature=0.2,
        max_output_tokens=700,
        strict_tools=strict,
    )


HELLO = (Message(Role.SYSTEM, "sys"), Message(Role.USER, "hola"))


# --------------------------------------------------------------------------- the request


def test_the_profiles_state_what_each_endpoint_accepts() -> None:
    assert PROFILES["openai"] == ProviderProfile(
        "openai", True, True, "max_completion_tokens", True
    )
    assert PROFILES["groq"] == ProviderProfile("groq", False, False, "max_tokens", False)
    assert PROFILES["generic"] == ProviderProfile("generic", False, False, "max_tokens", False)


def test_the_openai_request_offers_wrapped_tools_one_at_a_time() -> None:
    client = replying()

    adapter(client).complete(HELLO, ALL_TOOLS)

    (sent,) = client.sent
    assert sent["model"] == "gpt-4o-mini"
    assert sent["temperature"] == 0.2
    assert sent["max_completion_tokens"] == 700
    assert "max_tokens" not in sent
    assert sent["tool_choice"] == "auto"
    assert sent["parallel_tool_calls"] is False
    assert [tool["type"] for tool in sent["tools"]] == ["function"] * 3
    first = sent["tools"][0]["function"]
    assert set(first) == {"name", "description", "parameters"}  # strict is off by default
    assert first["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("profile", ["groq", "generic"])
def test_groq_and_generic_send_neither_strict_nor_the_parallel_flag_and_use_max_tokens(
    profile: str,
) -> None:
    client = replying()

    adapter(client, profile, strict=True).complete(HELLO, ALL_TOOLS)

    (sent,) = client.sent
    assert sent["max_tokens"] == 700
    assert "max_completion_tokens" not in sent
    assert "parallel_tool_calls" not in sent
    assert sent["tool_choice"] == "auto"
    assert all("strict" not in tool["function"] for tool in sent["tools"])


@pytest.mark.parametrize("profile", ["openai", "groq", "generic"])
def test_without_tools_neither_tools_nor_tool_choice_nor_the_parallel_flag_are_sent(
    profile: str,
) -> None:
    client = replying()

    adapter(client, profile).complete(HELLO, ())

    (sent,) = client.sent
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(sent)


def test_strict_tools_send_a_schema_without_the_keywords_strict_mode_rejects() -> None:
    client = replying()

    adapter(client, strict=True).complete(HELLO, ALL_TOOLS)

    (sent,) = client.sent
    functions = [tool["function"] for tool in sent["tools"]]
    assert all(function["strict"] is True for function in functions)
    text = json.dumps(functions)
    for keyword in ("minLength", "maxLength", "pattern", "title", "default"):
        assert f'"{keyword}"' not in text
    sales = next(f for f in functions if f["name"] == "consultar_ventas")
    assert sales["parameters"]["required"] == ["fecha"]
    assert sales["parameters"]["additionalProperties"] is False


def test_without_strict_the_schema_keeps_the_constraints_the_provider_may_use() -> None:
    client = replying()

    adapter(client, strict=False).complete(HELLO, ALL_TOOLS)

    text = json.dumps(client.sent[0]["tools"])
    assert '"pattern"' in text
    assert '"minLength"' in text


def test_sending_a_sanitized_schema_does_not_change_the_tool_contract_itself() -> None:
    spec: ToolSpec = TOOL_SPECS["consultar_ventas"]
    before = spec.args_model.model_json_schema()

    adapter(replying(), strict=True).complete(HELLO, (spec,))

    assert spec.args_model.model_json_schema() == before
    assert "pattern" in json.dumps(before)


# --------------------------------------------------------------------------- the messages


def wire_messages(messages: tuple[Message, ...], profile: str = "openai") -> list[dict[str, Any]]:
    client = replying()
    adapter(client, profile).complete(messages, ())
    sent: list[dict[str, Any]] = client.sent[0]["messages"]
    return sent


def test_every_role_maps_to_its_wire_shape() -> None:
    call = ToolCall("call-1", "consultar_ventas", '{"fecha": "2026-10-22"}')
    history = (
        Message(Role.SYSTEM, "sys"),
        Message(Role.USER, "ventas de ayer?"),
        Message(Role.ASSISTANT, None, (call,)),
        Message(Role.TOOL, '{"status": "ok"}', tool_call_id="call-1"),
        Message(Role.ASSISTANT, "Vendiste 7 entradas."),
    )

    assert wire_messages(history) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ventas de ayer?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "consultar_ventas",
                        "arguments": '{"fecha": "2026-10-22"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"status": "ok"}'},
        {"role": "assistant", "content": "Vendiste 7 entradas."},
    ]


@pytest.mark.parametrize("profile", ["openai", "groq", "generic"])
def test_a_platform_note_goes_out_as_a_system_message_in_its_place(profile: str) -> None:
    call = ToolCall("call-1", "crear_reclamo", '{"motivo": "M", "descripcion": "D"}')
    note = note_text(Withdrawal.INVALIDATED)
    history = (
        Message(Role.SYSTEM, "sys"),
        Message(Role.USER, "quiero reclamar"),
        Message(Role.ASSISTANT, None, (call,)),
        Message(Role.TOOL, '{"status": "pending_confirmation"}', tool_call_id="call-1"),
        Message(Role.ASSISTANT, "Voy a registrar esto. Respondé sí."),
        Message(Role.USER, "perdón, el evento es otro"),
        Message(Role.SYSTEM, note),
    )

    wire = wire_messages(history, profile)

    assert [(item["role"], item.get("content")) for item in wire][-2:] == [
        ("user", "perdón, el evento es otro"),
        ("system", note),
    ]
    assert [item["role"] for item in wire] == [
        "system", "user", "assistant", "tool", "assistant", "user", "system"
    ]  # fmt: skip
    assert all(set(item) == {"role", "content"} for item in (wire[0], wire[-1]))


def test_an_assistant_message_with_text_and_calls_keeps_both() -> None:
    call = ToolCall("c1", "buscar_documentacion", "{}")

    (wire,) = wire_messages((Message(Role.ASSISTANT, "Busco.", (call,)),))

    assert wire["content"] == "Busco."
    assert wire["tool_calls"][0]["id"] == "c1"


def test_an_operator_message_is_sent_as_an_assistant_message_named_operator() -> None:
    operator = Message(Role.ASSISTANT, "Ya lo reviso.", author=Author.OPERATOR)

    (wire,) = wire_messages((operator,), "openai")

    assert wire == {"role": "assistant", "content": "Ya lo reviso.", "name": "operator"}


@pytest.mark.parametrize("profile", ["groq", "generic"])
def test_no_name_is_sent_to_an_endpoint_that_does_not_support_it(profile: str) -> None:
    operator = Message(Role.ASSISTANT, "Ya lo reviso.", author=Author.OPERATOR)
    messages = (Message(Role.USER, "hola"), operator)

    wire = wire_messages(messages, profile)

    assert wire[1] == {"role": "assistant", "content": "Ya lo reviso."}
    assert all("name" not in item for item in wire)


def test_an_agent_message_never_carries_a_name() -> None:
    (wire,) = wire_messages((Message(Role.ASSISTANT, "Hola."),), "openai")

    assert "name" not in wire


# --------------------------------------------------------------------------- the response


def test_a_text_reply_carries_the_served_model_and_the_usage() -> None:
    client = replying(answer("La reserva dura 12 minutos.", model="gpt-4o-mini-2024-07-18"))

    completion = adapter(client).complete(HELLO, ())

    assert completion.reply.text == "La reserva dura 12 minutos."
    assert completion.reply.tool_calls == ()
    assert completion.model == "gpt-4o-mini-2024-07-18"
    assert (completion.prompt_tokens, completion.completion_tokens) == (11, 7)


def test_tool_calls_keep_their_ids_names_and_raw_arguments() -> None:
    raw = '{"fecha": "2026-10-22", "extra": 1}'  # untrusted: validating it is the guards' job
    response = WireCompletion(
        [
            WireChoice(
                WireMessage(tool_calls=[WireCall("call_a", Function("consultar_ventas", raw))]),
                finish_reason="tool_calls",
            )
        ]
    )

    completion = adapter(replying(response)).complete(HELLO, ALL_TOOLS)

    assert completion.reply.text is None
    assert completion.reply.tool_calls == (ToolCall("call_a", "consultar_ventas", raw),)


def test_missing_usage_is_none_and_not_a_failure() -> None:
    completion = adapter(replying(answer(usage=None))).complete(HELLO, ())

    assert (completion.prompt_tokens, completion.completion_tokens) == (None, None)


def test_a_reply_cut_by_the_token_limit_is_incomplete_even_with_text() -> None:
    response = WireCompletion(
        [WireChoice(WireMessage(content="La reserva du"), finish_reason="length")]
    )

    with pytest.raises(ModelIncomplete) as raised:
        adapter(replying(response)).complete(HELLO, ())

    assert raised.value.code == "model_incomplete"


def test_a_truncated_tool_call_is_incomplete_not_executed() -> None:
    cut = WireCompletion(
        [
            WireChoice(
                WireMessage(tool_calls=[WireCall("c1", Function("consultar_ventas", '{"fec'))]),
                finish_reason="length",
            )
        ]
    )

    with pytest.raises(ModelIncomplete):
        adapter(replying(cut)).complete(HELLO, ALL_TOOLS)


def test_the_content_filter_is_a_refusal() -> None:
    response = WireCompletion(
        [WireChoice(WireMessage(content=None), finish_reason="content_filter")]
    )

    with pytest.raises(ModelRefused) as raised:
        adapter(replying(response)).complete(HELLO, ())

    assert raised.value.code == "model_refused"


def test_a_refusal_field_is_a_refusal_whatever_the_finish_reason() -> None:
    response = WireCompletion([WireChoice(WireMessage(content=None, refusal="I can't help."))])

    with pytest.raises(ModelRefused):
        adapter(replying(response)).complete(HELLO, ())


def test_a_blank_refusal_field_is_not_a_refusal() -> None:
    response = WireCompletion([WireChoice(WireMessage(content="Hola.", refusal=" "))])

    assert adapter(replying(response)).complete(HELLO, ()).reply.text == "Hola."


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_an_empty_reply_with_no_tool_calls_is_incomplete(content: str | None) -> None:
    response = WireCompletion([WireChoice(WireMessage(content=content))])

    with pytest.raises(ModelIncomplete):
        adapter(replying(response)).complete(HELLO, ())


def test_a_reply_without_choices_is_incomplete() -> None:
    with pytest.raises(ModelIncomplete):
        adapter(replying(WireCompletion([]))).complete(HELLO, ())


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param([CustomCall()], id="custom call"),
        pytest.param([WireCall("", Function("x", "{}"))], id="blank id"),
        pytest.param(
            [WireCall("same", Function("x", "{}")), WireCall("same", Function("y", "{}"))],
            id="duplicate ids",
        ),
        pytest.param([object()], id="no function at all"),
    ],
)
def test_a_malformed_tool_call_makes_the_reply_incomplete(calls: list[Any]) -> None:
    response = WireCompletion(
        [WireChoice(WireMessage(tool_calls=calls), finish_reason="tool_calls")]
    )

    with pytest.raises(ModelIncomplete):
        adapter(replying(response)).complete(HELLO, ALL_TOOLS)


def test_a_served_model_that_is_not_the_requested_one_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = replying(answer(model="gpt-3.5-turbo"))

    with caplog.at_level(logging.WARNING):
        completion = adapter(client).complete(HELLO, ())

    assert completion.model == "gpt-3.5-turbo"
    assert any(
        "gpt-4o-mini" in r.getMessage() and "gpt-3.5-turbo" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("served", ["gpt-4o-mini", "gpt-4o-mini-2024-07-18"])
def test_a_dated_snapshot_of_the_requested_model_is_not_a_mismatch(
    caplog: pytest.LogCaptureFixture, served: str
) -> None:
    with caplog.at_level(logging.WARNING):
        adapter(replying(answer(model=served))).complete(HELLO, ())

    assert caplog.records == []


# --------------------------------------------------------------------------- the failures


def status_error(kind: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=REQUEST)
    return kind(f"Incorrect API key provided: {FAKE_KEY}", response=response, body=None)


FAILURES: list[tuple[str, Callable[[], Exception], type[ModelError], str]] = [
    (
        "timeout",
        lambda: openai.APITimeoutError(request=REQUEST),
        ModelUnavailable,
        "APITimeoutError",
    ),
    (
        "connection",
        lambda: openai.APIConnectionError(message=FAKE_KEY, request=REQUEST),
        ModelUnavailable,
        "APIConnectionError",
    ),
    (
        "rate limit",
        lambda: status_error(openai.RateLimitError, 429),
        ModelUnavailable,
        "RateLimitError status=429",
    ),
    (
        "server error",
        lambda: status_error(openai.InternalServerError, 503),
        ModelUnavailable,
        "InternalServerError status=503",
    ),
    (
        "request timeout status",
        lambda: status_error(openai.APIStatusError, 408),
        ModelUnavailable,
        "APIStatusError status=408",
    ),
    (
        "authentication",
        lambda: status_error(openai.AuthenticationError, 401),
        ModelAuthError,
        "AuthenticationError status=401",
    ),
    (
        "permission",
        lambda: status_error(openai.PermissionDeniedError, 403),
        ModelAuthError,
        "PermissionDeniedError status=403",
    ),
    (
        "not found",
        lambda: status_error(openai.NotFoundError, 404),
        ModelNotFound,
        "NotFoundError status=404",
    ),
    (
        "bad request",
        lambda: status_error(openai.BadRequestError, 400),
        ModelRequestRejected,
        "BadRequestError status=400",
    ),
    (
        "unprocessable",
        lambda: status_error(openai.UnprocessableEntityError, 422),
        ModelRequestRejected,
        "UnprocessableEntityError status=422",
    ),
    (
        "other status",
        lambda: status_error(openai.APIStatusError, 418),
        ModelRequestRejected,
        "APIStatusError status=418",
    ),
    (
        "other api error",
        lambda: openai.APIError(FAKE_KEY, REQUEST, body=None),
        ModelRequestRejected,
        "APIError",
    ),
    (
        "other sdk error",
        lambda: openai.OpenAIError(FAKE_KEY),
        ModelRequestRejected,
        "OpenAIError",
    ),
]


@pytest.mark.parametrize(
    ("make", "expected", "detail"),
    [pytest.param(m, e, d, id=name) for name, m, e, d in FAILURES],
)
def test_every_sdk_failure_becomes_the_right_domain_error_without_the_sdk_text(
    make: Callable[[], Exception], expected: type[ModelError], detail: str
) -> None:
    client = replying(error=make())

    with pytest.raises(expected) as raised:
        adapter(client).complete(HELLO, ())

    error = raised.value
    assert type(error) is expected
    assert error.detail == detail
    assert FAKE_KEY not in json.dumps(error.public())
    assert FAKE_KEY not in error.detail
    assert FAKE_KEY not in str(error)
    # The SDK exception is not chained, so a logged traceback cannot quote it either.
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert FAKE_KEY not in "".join(traceback.format_exception(error))


def test_only_the_transient_failures_are_retryable_and_each_code_is_stable() -> None:
    assert ModelUnavailable.retryable is True
    codes = {
        cls.code: cls.retryable
        for cls in (
            ModelUnavailable,
            ModelAuthError,
            ModelNotFound,
            ModelRequestRejected,
            ModelIncomplete,
            ModelRefused,
        )
    }
    assert codes == {
        "model_unavailable": True,
        "model_auth": False,
        "model_not_found": False,
        "model_rejected": False,
        "model_incomplete": False,
        "model_refused": False,
    }
    assert issubclass(ModelError, DomainError)


def test_the_failure_is_logged_with_its_class_and_status_but_not_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = replying(error=status_error(openai.AuthenticationError, 401))

    with caplog.at_level(logging.WARNING), pytest.raises(ModelAuthError):
        adapter(client).complete(HELLO, ())

    text = " ".join(record.getMessage() for record in caplog.records)
    assert "model_auth" in text
    assert "AuthenticationError status=401" in text
    assert FAKE_KEY not in text


def test_an_error_that_is_not_the_sdks_is_not_hidden() -> None:
    # A bug in our own code must stay loud: the conversation turns it into a safe reply.
    with pytest.raises(RuntimeError):
        adapter(replying(error=RuntimeError("bug"))).complete(HELLO, ())


def test_the_adapter_makes_one_call_per_completion_and_never_retries_by_itself() -> None:
    client = replying(error=openai.APITimeoutError(request=REQUEST))

    with pytest.raises(ModelUnavailable):
        adapter(client).complete(HELLO, ())

    assert len(client.sent) == 1


def test_a_blank_model_or_a_zero_budget_is_refused_at_construction() -> None:
    client = replying()
    with pytest.raises(ValueError, match="model name"):
        adapter(client, model=" ")
    with pytest.raises(ValueError, match="max_output_tokens"):
        OpenAIChatModel(
            client, "m", profile=PROFILES["openai"], temperature=0.2, max_output_tokens=0
        )
