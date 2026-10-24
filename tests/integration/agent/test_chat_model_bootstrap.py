"""The composition root builds the chat model from the settings, offline."""

from pathlib import Path
from typing import Any

import openai
import pytest
from pydantic import SecretStr

from merchant_assistant.agent.adapters.errors import LLMNotConfigured
from merchant_assistant.agent.adapters.openai_chat_model import PROFILES, OpenAIChatModel
from merchant_assistant.bootstrap import build_chat_model
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

FAKE_OPENAI_KEY = "sk-FAKE-openai"
FAKE_LLM_KEY = "gsk-FAKE-llm"


class RecordingOpenAI:
    """Stands in for `openai.OpenAI`: remembers how it was built, opens no connection."""

    built: list[dict[str, Any]] = []  # noqa: RUF012 - a test double

    def __init__(self, **kwargs: Any) -> None:
        self.built.append(kwargs)


@pytest.fixture(autouse=True)
def recording_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    RecordingOpenAI.built = []
    monkeypatch.setattr(openai, "OpenAI", RecordingOpenAI)
    for name in ("OPENAI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return RecordingOpenAI.built


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, embeddings_cache_path=tmp_path / "e.sqlite", **overrides)


def test_the_default_build_talks_to_openai_with_the_sdk_retrying_twice(
    tmp_path: Path, recording_client: list[dict[str, Any]]
) -> None:
    model = build_chat_model(settings(tmp_path, openai_api_key=SecretStr(FAKE_OPENAI_KEY)))

    assert isinstance(model, OpenAIChatModel)
    assert recording_client == [
        {"api_key": FAKE_OPENAI_KEY, "base_url": None, "timeout": 30.0, "max_retries": 2}
    ]


def test_a_free_groq_profile_builds_against_its_endpoint_with_its_own_key(
    tmp_path: Path, recording_client: list[dict[str, Any]]
) -> None:
    configured = settings(
        tmp_path,
        openai_api_key=SecretStr(FAKE_OPENAI_KEY),
        llm_provider="groq",
        llm_base_url="https://api.groq.com/openai/v1",
        llm_api_key=SecretStr(FAKE_LLM_KEY),
        llm_model="openai/gpt-oss-120b",
        llm_timeout_seconds=12,
    )

    model = build_chat_model(configured)

    assert isinstance(model, OpenAIChatModel)
    assert recording_client == [
        {
            "api_key": FAKE_LLM_KEY,
            "base_url": "https://api.groq.com/openai/v1",
            "timeout": 12.0,
            "max_retries": 2,
        }
    ]
    assert model.build_request([], [])["max_tokens"] == 700  # the groq profile


def test_the_openai_key_is_not_sent_to_a_custom_endpoint(
    tmp_path: Path, recording_client: list[dict[str, Any]]
) -> None:
    configured = settings(
        tmp_path,
        openai_api_key=SecretStr(FAKE_OPENAI_KEY),
        llm_base_url="https://api.groq.com/openai/v1",
    )

    with pytest.raises(LLMNotConfigured):
        build_chat_model(configured)

    assert recording_client == []


def test_without_a_key_it_refuses_in_spanish_naming_variables_and_no_value(
    tmp_path: Path, recording_client: list[dict[str, Any]]
) -> None:
    with pytest.raises(LLMNotConfigured) as refused:
        build_chat_model(settings(tmp_path))

    error = refused.value
    assert error.code == "llm_not_configured"
    assert "LLM_API_KEY" in error.user_message
    assert "OPENAI_API_KEY" in error.user_message
    assert "sk-" not in error.user_message
    assert recording_client == []


def test_the_profile_and_the_knobs_of_the_settings_reach_the_adapter(tmp_path: Path) -> None:
    configured = settings(
        tmp_path,
        llm_api_key=SecretStr(FAKE_LLM_KEY),
        llm_provider="generic",
        llm_temperature=0.7,
        llm_max_output_tokens=123,
        llm_strict_tools=True,  # ignored: the generic profile does not accept it
    )

    request = build_chat_model(configured).build_request([], [])

    assert request["temperature"] == 0.7
    assert request["max_tokens"] == 123
    assert PROFILES["generic"].supports_strict_tools is False
