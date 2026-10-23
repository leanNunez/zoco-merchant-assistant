from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from merchant_assistant.shared.config import Settings

VARIABLES = ("OPENAI_API_KEY", "EMBEDDINGS_MODEL", "EMBEDDINGS_CACHE_PATH", "BUSINESS_TIMEZONE")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The developer's shell must not decide what these tests see.
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_without_environment_the_defaults_apply_and_there_is_no_key() -> None:
    settings = Settings(_env_file=None)

    assert settings.openai_api_key is None
    assert settings.embeddings_model == "text-embedding-3-small"
    assert settings.embeddings_cache_path == Path("data/cache/embeddings.sqlite")
    assert settings.business_timezone == "America/Argentina/Buenos_Aires"
    assert settings.timezone == ZoneInfo("America/Argentina/Buenos_Aires")


def test_values_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("EMBEDDINGS_CACHE_PATH", "var/other.sqlite")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "UTC")

    settings = Settings(_env_file=None)

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-test-not-a-real-key"
    assert settings.embeddings_model == "text-embedding-3-large"
    assert settings.embeddings_cache_path == Path("var/other.sqlite")
    assert settings.business_timezone == "UTC"


def test_values_come_from_an_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("EMBEDDINGS_MODEL=text-embedding-3-large\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.embeddings_model == "text-embedding-3-large"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_counts_as_no_key(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", blank)

    assert Settings(_env_file=None).openai_api_key is None


def test_the_key_never_appears_in_the_representation_of_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    settings = Settings(_env_file=None)

    assert "sk-test-not-a-real-key" not in repr(settings)
    assert "sk-test-not-a-real-key" not in str(settings.model_dump())


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "Buenos Aires", ""])
def test_an_unknown_timezone_stops_the_load_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    monkeypatch.setenv("BUSINESS_TIMEZONE", zone)

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)

    message = str(refused.value)
    assert f"BUSINESS_TIMEZONE '{zone}' is not an IANA timezone" in message
    assert "America/Argentina/Buenos_Aires" in message


def test_a_blank_model_name_stops_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDINGS_MODEL", " ")

    with pytest.raises(ValidationError, match="EMBEDDINGS_MODEL must name a model"):
        Settings(_env_file=None)


def test_unrelated_variables_in_the_env_file_are_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")

    assert Settings(_env_file=env_file).embeddings_model == "text-embedding-3-small"
