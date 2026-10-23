from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from merchant_assistant.shared.config import Settings
from merchant_assistant.shared.faults import FaultConfig, FaultMode

VARIABLES = (
    "OPENAI_API_KEY",
    "EMBEDDINGS_MODEL",
    "EMBEDDINGS_CACHE_PATH",
    "BUSINESS_TIMEZONE",
    "TOOL_FAILURE_RATE",
    "TOOL_FAILURE_SEED",
    "TOOL_FAILURE_MODES",
    "SALES_PATH",
    "COMPLAINTS_DB_PATH",
)


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


def test_failures_are_off_and_the_data_lives_in_the_default_places() -> None:
    settings = Settings(_env_file=None)

    assert settings.fault_config() == FaultConfig(
        rate=0.0, seed=0, modes=frozenset({FaultMode.TIMEOUT, FaultMode.ERROR, FaultMode.LOST_ACK})
    )
    assert settings.sales_path == Path("data/ventas.json")
    assert settings.complaints_db_path == Path("var/reclamos.db")


def test_the_failure_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_FAILURE_RATE", "0.25")
    monkeypatch.setenv("TOOL_FAILURE_SEED", "42")
    monkeypatch.setenv("TOOL_FAILURE_MODES", "timeout, lost_ack")
    monkeypatch.setenv("SALES_PATH", "elsewhere/ventas.json")
    monkeypatch.setenv("COMPLAINTS_DB_PATH", "elsewhere/reclamos.db")

    settings = Settings(_env_file=None)

    assert settings.fault_config() == FaultConfig(
        rate=0.25, seed=42, modes=frozenset({FaultMode.TIMEOUT, FaultMode.LOST_ACK})
    )
    assert settings.sales_path == Path("elsewhere/ventas.json")
    assert settings.complaints_db_path == Path("elsewhere/reclamos.db")


@pytest.mark.parametrize(
    ("csv", "expected"),
    [
        ("error", {FaultMode.ERROR}),
        (" timeout , error ", {FaultMode.TIMEOUT, FaultMode.ERROR}),
        ("error,error", {FaultMode.ERROR}),
        ("timeout,,error,", {FaultMode.TIMEOUT, FaultMode.ERROR}),
    ],
)
def test_the_modes_are_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch, csv: str, expected: set[FaultMode]
) -> None:
    monkeypatch.setenv("TOOL_FAILURE_MODES", csv)

    assert Settings(_env_file=None).fault_config().modes == frozenset(expected)


def test_an_unknown_mode_stops_the_load_and_lists_the_allowed_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOOL_FAILURE_MODES", "timeout,explode")

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)

    message = str(refused.value)
    assert "unknown mode(s) explode" in message
    assert "allowed: timeout, error, lost_ack" in message


@pytest.mark.parametrize("rate", ["-0.1", "1.5", "nan", "inf"])
def test_a_failure_rate_outside_zero_to_one_stops_the_load(
    monkeypatch: pytest.MonkeyPatch, rate: str
) -> None:
    monkeypatch.setenv("TOOL_FAILURE_RATE", rate)

    with pytest.raises(ValidationError, match="TOOL_FAILURE_RATE must be between 0 and 1"):
        Settings(_env_file=None)


def test_a_non_numeric_rate_or_seed_stops_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_FAILURE_RATE", "often")
    with pytest.raises(ValidationError, match="tool_failure_rate"):
        Settings(_env_file=None)

    monkeypatch.setenv("TOOL_FAILURE_RATE", "0.1")
    monkeypatch.setenv("TOOL_FAILURE_SEED", "abc")
    with pytest.raises(ValidationError, match="tool_failure_seed"):
        Settings(_env_file=None)


def test_failures_with_no_mode_stop_the_load_but_no_failures_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOOL_FAILURE_MODES", "")

    assert Settings(_env_file=None).fault_config().modes == frozenset()

    monkeypatch.setenv("TOOL_FAILURE_RATE", "0.5")
    with pytest.raises(ValidationError, match="at least one mode"):
        Settings(_env_file=None)
