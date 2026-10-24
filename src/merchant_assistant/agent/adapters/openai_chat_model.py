"""The chat model over the OpenAI Chat Completions API, or any endpoint that speaks it.

The client is taken as an argument (anything shaped like `openai.OpenAI`), so the tests pass a
hand-written fake and never touch the network. What differs between endpoints is captured by a
`ProviderProfile`: Groq, for instance, rejects `parallel_tool_calls` and `strict`, and expects
`max_tokens`. A profile is a statement of what an endpoint is known to accept, so an unknown
endpoint gets the conservative `generic` one.

Policy lives elsewhere (ADR-0001): this adapter only translates. Every failure becomes a
`ModelError` whose public view never includes the SDK's text, and the SDK's own retries are the
only retries (the client is built with `max_retries` in `bootstrap.py`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

import openai

from merchant_assistant.agent.adapters.errors import (
    ModelAuthError,
    ModelError,
    ModelIncomplete,
    ModelNotFound,
    ModelRefused,
    ModelRequestRejected,
    ModelUnavailable,
)
from merchant_assistant.agent.domain.errors import InvalidMessage
from merchant_assistant.agent.domain.messages import Author, Message, ModelReply, Role, ToolCall
from merchant_assistant.agent.domain.tools import ToolSpec, json_schema
from merchant_assistant.agent.ports import Completion

logger = logging.getLogger(__name__)

# Keywords strict mode rejects. Our own validation (`agent/domain/tools.py`) still enforces
# every one of them: only the schema SENT to the provider loses them.
_STRICT_UNSUPPORTED = frozenset({"minLength", "maxLength", "pattern", "title", "default"})
_RETRYABLE_STATUS = frozenset({408, 409, 429})
_UNKNOWN_MODEL = "unknown"
_OPERATOR_NAME = "operator"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """What an endpoint is known to accept."""

    name: str
    supports_strict_tools: bool
    supports_parallel_tool_calls_flag: bool
    token_limit_param: Literal["max_completion_tokens", "max_tokens"]
    supports_message_name: bool


PROFILES: Mapping[str, ProviderProfile] = MappingProxyType(
    {
        "openai": ProviderProfile("openai", True, True, "max_completion_tokens", True),
        "groq": ProviderProfile("groq", False, False, "max_tokens", False),
        "generic": ProviderProfile("generic", False, False, "max_tokens", False),
    }
)


# ---------------------------------------------------------------------- the client's shape
# Only what the adapter reads. The SDK's own types satisfy them; so does a plain fake.


class _Usage(Protocol):
    @property
    def prompt_tokens(self) -> int: ...

    @property
    def completion_tokens(self) -> int: ...


class _ChoiceMessage(Protocol):
    @property
    def content(self) -> str | None: ...

    @property
    def refusal(self) -> str | None: ...

    # The SDK types each call as a union (function or custom), so the items are read defensively.
    @property
    def tool_calls(self) -> Sequence[Any] | None: ...


class _Choice(Protocol):
    @property
    def finish_reason(self) -> str: ...

    @property
    def message(self) -> _ChoiceMessage: ...


class _ChatCompletion(Protocol):
    @property
    def choices(self) -> Sequence[_Choice]: ...

    @property
    def model(self) -> str: ...

    @property
    def usage(self) -> _Usage | None: ...


class _Completions(Protocol):
    def create(self, *args: Any, **kwargs: Any) -> _ChatCompletion: ...


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...


class ChatClient(Protocol):
    @property
    def chat(self) -> _Chat: ...


# ---------------------------------------------------------------------- the adapter


class OpenAIChatModel:
    def __init__(
        self,
        client: ChatClient,
        model: str,
        *,
        profile: ProviderProfile,
        temperature: float,
        max_output_tokens: int,
        strict_tools: bool = False,
    ) -> None:
        if not model.strip():
            raise ValueError("a model name is required")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be >= 1")
        self._client = client
        self._model = model
        self._profile = profile
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        # Strict schemas only when asked for AND the endpoint is known to accept them.
        self._strict = strict_tools and profile.supports_strict_tools

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        request = self.build_request(messages, tools)
        try:
            response = self._client.chat.completions.create(**request)
        except openai.OpenAIError as failure:
            error = _map_failure(failure)
            logger.warning("chat model call failed: %s (%s)", error.code, error.detail)
            raise error from None
        return self._completion(response)

    # ------------------------------------------------------------------ request

    def build_request(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message(message) for message in messages],
            "temperature": self._temperature,
            self._profile.token_limit_param: self._max_output_tokens,
        }
        if tools:
            request["tools"] = [self._tool(spec) for spec in tools]
            request["tool_choice"] = "auto"
            if self._profile.supports_parallel_tool_calls_flag:
                # Each effect must cross the confirmation gate alone.
                request["parallel_tool_calls"] = False
        return request

    def _tool(self, spec: ToolSpec) -> dict[str, object]:
        declaration = json_schema(spec)
        function: dict[str, object] = {
            "name": declaration["name"],
            "description": declaration["description"],
            "parameters": _strip_for_strict(declaration["parameters"])
            if self._strict
            else declaration["parameters"],
        }
        if self._strict:
            function["strict"] = True
        return {"type": "function", "function": function}

    def _message(self, message: Message) -> dict[str, object]:
        match message.role:
            case Role.TOOL:
                return {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            case Role.ASSISTANT:
                wire: dict[str, object] = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    wire["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in message.tool_calls
                    ]
                # A person of the team speaks as the assistant: to the model it is the merchant's
                # side of the conversation, labelled where the endpoint allows it.
                if message.author is Author.OPERATOR and self._profile.supports_message_name:
                    wire["name"] = _OPERATOR_NAME
                return wire
            case Role.SYSTEM | Role.USER:
                return {"role": message.role.value, "content": message.content}

    # ------------------------------------------------------------------ response

    def _completion(self, response: _ChatCompletion) -> Completion:
        if not response.choices:
            raise ModelIncomplete("no choices in the reply")
        choice = response.choices[0]
        message = choice.message
        if choice.finish_reason == "length":
            raise ModelIncomplete("finish_reason=length")
        if choice.finish_reason == "content_filter":
            raise ModelRefused("finish_reason=content_filter")
        if message.refusal is not None and message.refusal.strip():
            raise ModelRefused("the model returned a refusal")
        calls = tuple(_tool_call(raw) for raw in message.tool_calls or ())
        try:
            reply = ModelReply(message.content, calls)
        except InvalidMessage as malformed:
            raise ModelIncomplete(f"unusable reply: {malformed.code}") from None
        served = response.model.strip() if isinstance(response.model, str) else ""
        served = served or _UNKNOWN_MODEL
        if not served.startswith(self._model):
            logger.warning("asked for model %r but %r answered", self._model, served)
        usage = response.usage
        return Completion(
            reply,
            served,
            _count(None if usage is None else usage.prompt_tokens),
            _count(None if usage is None else usage.completion_tokens),
        )


def _tool_call(raw: object) -> ToolCall:
    function = getattr(raw, "function", None)
    call_id = getattr(raw, "id", None)
    name = getattr(function, "name", None)
    arguments = getattr(function, "arguments", "")
    if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
        # For instance a custom tool call: this agent offers function tools only.
        raise ModelIncomplete("a tool call without an id, a name or text arguments")
    try:
        return ToolCall(call_id, name, arguments)
    except InvalidMessage:
        raise ModelIncomplete("a tool call without a usable id") from None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _strip_for_strict(node: object) -> Any:
    """The schema without the keywords strict mode refuses, at every depth."""
    if isinstance(node, dict):
        return {
            key: _strip_for_strict(value)
            for key, value in node.items()
            if key not in _STRICT_UNSUPPORTED
        }
    if isinstance(node, list):
        return [_strip_for_strict(item) for item in node]
    return node


def _map_failure(failure: openai.OpenAIError) -> ModelError:
    """The SDK's exception as a domain error. Only the class name and the status are kept."""
    name = type(failure).__name__
    if isinstance(failure, openai.APIStatusError):
        status = failure.status_code
        detail = f"{name} status={status}"
        if isinstance(failure, openai.AuthenticationError | openai.PermissionDeniedError):
            return ModelAuthError(detail)
        if isinstance(failure, openai.NotFoundError):
            return ModelNotFound(detail)
        if status >= 500 or status in _RETRYABLE_STATUS:
            return ModelUnavailable(detail)
        return ModelRequestRejected(detail)
    if isinstance(failure, openai.APIConnectionError):  # also the timeouts
        return ModelUnavailable(name)
    return ModelRequestRejected(name)
