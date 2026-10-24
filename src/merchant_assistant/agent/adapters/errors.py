"""Why a chat model could not answer, as the agent's own errors.

Each error has a stable `code` and a safe `user_message`. The `detail` is for the logs only and
carries just the SDK class name and the HTTP status: an SDK message can quote part of an API
key (a 401 often does), so it is never copied into an error, a log line or a reply.

`retryable` says whether trying again later can help; the SDK has already retried by itself
(`max_retries` of the client), so the agent does not add a second retry layer.
"""

from __future__ import annotations

from typing import ClassVar

from merchant_assistant.agent.domain.errors import AgentError


class ModelError(AgentError):
    """Base of every failure of the chat model."""

    retryable: ClassVar[bool] = False


class ModelUnavailable(ModelError):  # noqa: N818 - named after the situation
    """The provider could not be reached or is overloaded: a timeout, a 5xx or a 429."""

    code: ClassVar[str] = "model_unavailable"
    user_message: ClassVar[str] = "The language model is temporarily unavailable."
    retryable: ClassVar[bool] = True


class ModelAuthError(ModelError):
    """The provider refused the credentials. Retrying cannot help: a person must fix the key."""

    code: ClassVar[str] = "model_auth"
    user_message: ClassVar[str] = "The language model refused the configured credentials."


class ModelNotFound(ModelError):  # noqa: N818 - named after the situation
    """The provider does not know the model (or the endpoint): it may have been retired."""

    code: ClassVar[str] = "model_not_found"
    user_message: ClassVar[str] = "The configured language model was not found."


class ModelRequestRejected(ModelError):  # noqa: N818 - named after the situation
    """The provider refused the request itself (a bad parameter, a malformed tool call)."""

    code: ClassVar[str] = "model_rejected"
    user_message: ClassVar[str] = "The language model rejected the request."


class ModelIncomplete(ModelError):  # noqa: N818 - named after the situation
    """The reply was cut short or empty or malformed, so it is not safe to use."""

    code: ClassVar[str] = "model_incomplete"
    user_message: ClassVar[str] = "The language model did not finish a usable reply."


class ModelRefused(ModelError):  # noqa: N818 - named after the situation
    """The model (or the provider's filter) declined to answer."""

    code: ClassVar[str] = "model_refused"
    user_message: ClassVar[str] = "The language model declined to answer."


class LLMNotConfigured(AgentError):  # noqa: N818 - named after the situation
    """There is no key for the chat model. Names the variables to set, never a value."""

    code: ClassVar[str] = "llm_not_configured"
    user_message: ClassVar[str] = (
        "El asistente no tiene un modelo de lenguaje configurado. Definí LLM_API_KEY en el "
        "archivo .env (o OPENAI_API_KEY si usás OpenAI sin LLM_BASE_URL) y volvé a abrir la "
        "consola."
    )
