from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr, ValidationError

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
    "DOCS_DIR",
    "HUMAN_HOURS_START",
    "HUMAN_HOURS_END",
    "HUMAN_HOURS_DAYS",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_TEMPERATURE",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_OUTPUT_TOKENS",
    "LLM_STRICT_TOOLS",
    "OPERATOR_PIN",
    "SUPERVISOR_PIN",
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


# --------------------------------------------------------------------------- attention window


def test_the_attention_window_defaults_to_weekdays_from_nine_to_six() -> None:
    settings = Settings(_env_file=None)

    assert (settings.human_hours_start, settings.human_hours_end) == (time(9, 0), time(18, 0))
    assert settings.human_weekdays == frozenset({0, 1, 2, 3, 4})
    assert settings.docs_dir == Path("docs")


def test_the_attention_window_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUMAN_HOURS_START", "10:30")
    monkeypatch.setenv("HUMAN_HOURS_END", "14:00")
    monkeypatch.setenv("HUMAN_HOURS_DAYS", " SAT , sun ")
    monkeypatch.setenv("DOCS_DIR", "elsewhere/docs")

    settings = Settings(_env_file=None)

    assert (settings.human_hours_start, settings.human_hours_end) == (time(10, 30), time(14, 0))
    assert settings.human_weekdays == frozenset({5, 6})
    assert settings.docs_dir == Path("elsewhere/docs")


@pytest.mark.parametrize(
    ("days", "message"),
    [
        pytest.param("", "HUMAN_HOURS_DAYS must list at least one day", id="empty"),
        pytest.param(" , ", "HUMAN_HOURS_DAYS must list at least one day", id="only commas"),
        pytest.param("mon,lunes", "HUMAN_HOURS_DAYS has unknown day(s) lunes", id="spanish name"),
        pytest.param("monday", "HUMAN_HOURS_DAYS has unknown day(s) monday", id="long name"),
        pytest.param("mon,Mon", "HUMAN_HOURS_DAYS repeats mon", id="repeated"),
    ],
)
def test_a_wrong_list_of_days_stops_the_load_and_says_what_is_allowed(
    monkeypatch: pytest.MonkeyPatch, days: str, message: str
) -> None:
    monkeypatch.setenv("HUMAN_HOURS_DAYS", days)

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)

    assert message in str(refused.value)
    if "repeats" not in message:
        assert "mon, tue, wed, thu, fri, sat, sun" in str(refused.value)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param("18:00", "09:00", id="reversed"),
        pytest.param("09:00", "09:00", id="empty window"),
        pytest.param("22:00", "02:00", id="across midnight"),
    ],
)
def test_a_window_that_does_not_open_before_it_closes_stops_the_load(
    monkeypatch: pytest.MonkeyPatch, start: str, end: str
) -> None:
    monkeypatch.setenv("HUMAN_HOURS_START", start)
    monkeypatch.setenv("HUMAN_HOURS_END", end)

    with pytest.raises(ValidationError, match="must be earlier than HUMAN_HOURS_END"):
        Settings(_env_file=None)


def test_a_time_with_an_offset_is_refused_because_the_window_is_in_the_business_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUMAN_HOURS_START", "09:00-03:00")

    with pytest.raises(ValidationError, match="write them without an offset"):
        Settings(_env_file=None)


def test_a_time_that_is_not_a_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUMAN_HOURS_END", "seis de la tarde")

    with pytest.raises(ValidationError, match="human_hours_end"):
        Settings(_env_file=None)


# --------------------------------------------------------------------------- the chat model

FAKE_KEY = "sk-FAKE-openai-key"
FAKE_LLM_KEY = "gsk-FAKE-llm-key"
FAKE_PIN = "482913"


def test_the_chat_model_defaults_to_openai_with_cautious_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_base_url is None
    assert settings.llm_api_key is None
    assert settings.llm_temperature == 0.2
    assert settings.llm_timeout_seconds == 30
    assert settings.llm_max_output_tokens == 700
    assert settings.llm_strict_tools is False
    assert settings.operator_pin is None
    assert settings.supervisor_pin is None


def test_the_chat_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", " openai/gpt-oss-120b ")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("LLM_STRICT_TOOLS", "true")

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "groq"
    assert settings.llm_model == "openai/gpt-oss-120b"
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_temperature == 0
    assert settings.llm_timeout_seconds == 12.5
    assert settings.llm_max_output_tokens == 256
    assert settings.llm_strict_tools is True


def test_an_unknown_provider_stops_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")

    with pytest.raises(ValidationError, match="llm_provider"):
        Settings(_env_file=None)


@pytest.mark.parametrize("temperature", ["-0.1", "2.1", "nan", "inf"])
def test_a_temperature_outside_zero_to_two_stops_the_load(
    monkeypatch: pytest.MonkeyPatch, temperature: str
) -> None:
    monkeypatch.setenv("LLM_TEMPERATURE", temperature)

    with pytest.raises(ValidationError, match="LLM_TEMPERATURE must be between 0 and 2"):
        Settings(_env_file=None)


@pytest.mark.parametrize("temperature", ["0", "2", "1.3"])
def test_the_ends_of_the_temperature_range_are_valid(
    monkeypatch: pytest.MonkeyPatch, temperature: str
) -> None:
    monkeypatch.setenv("LLM_TEMPERATURE", temperature)

    assert Settings(_env_file=None).llm_temperature == float(temperature)


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_a_timeout_that_is_not_a_positive_number_stops_the_load(
    monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ValidationError, match="LLM_TIMEOUT_SECONDS must be a positive number"):
        Settings(_env_file=None)


@pytest.mark.parametrize("tokens", ["0", "-5"])
def test_an_output_budget_of_zero_or_less_stops_the_load(
    monkeypatch: pytest.MonkeyPatch, tokens: str
) -> None:
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", tokens)

    with pytest.raises(ValidationError, match="LLM_MAX_OUTPUT_TOKENS must be above 0"):
        Settings(_env_file=None)


def test_a_blank_model_or_a_url_without_a_scheme_stops_the_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "  ")
    with pytest.raises(ValidationError, match="LLM_MODEL must name a model"):
        Settings(_env_file=None)

    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("LLM_BASE_URL", "api.groq.com/openai/v1")
    with pytest.raises(ValidationError, match="LLM_BASE_URL must start with http"):
        Settings(_env_file=None)


@pytest.mark.parametrize("name", ["LLM_API_KEY", "LLM_BASE_URL", "OPERATOR_PIN", "SUPERVISOR_PIN"])
def test_a_blank_value_copied_from_a_template_counts_as_not_set(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert getattr(settings, name.lower()) is None


def test_the_llm_key_falls_back_to_the_openai_key_only_on_the_default_endpoint() -> None:
    plain = Settings(_env_file=None, openai_api_key=SecretStr(FAKE_KEY))
    custom = Settings(
        _env_file=None, openai_api_key=SecretStr(FAKE_KEY), llm_base_url="https://x.test/v1"
    )

    assert _revealed(plain) == FAKE_KEY
    assert _revealed(custom) is None  # never hand the OpenAI key to another endpoint


def test_an_explicit_llm_key_wins_with_or_without_a_custom_endpoint() -> None:
    for base_url in (None, "https://x.test/v1"):
        settings = Settings(
            _env_file=None,
            openai_api_key=SecretStr(FAKE_KEY),
            llm_api_key=SecretStr(FAKE_LLM_KEY),
            llm_base_url=base_url,
        )

        assert _revealed(settings) == FAKE_LLM_KEY


def test_with_no_key_at_all_there_is_no_effective_key() -> None:
    assert Settings(_env_file=None).effective_llm_api_key() is None


def test_no_secret_appears_in_the_representation_of_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("OPERATOR_PIN", FAKE_PIN)
    monkeypatch.setenv("SUPERVISOR_PIN", "771204")

    settings = Settings(_env_file=None)
    shown = " ".join(
        (
            repr(settings),
            str(settings),
            str(settings.model_dump()),
            repr(settings.model_dump_json()),
        )
    )
    wrapped = repr(settings.effective_llm_api_key())

    for secret in (FAKE_KEY, FAKE_LLM_KEY, FAKE_PIN, "771204"):
        assert secret not in shown
        assert secret not in wrapped


def test_a_pin_is_kept_as_a_secret_and_compares_by_value() -> None:
    settings = Settings(_env_file=None, operator_pin=SecretStr(FAKE_PIN))

    assert settings.operator_pin is not None
    assert settings.operator_pin.get_secret_value() == FAKE_PIN
    assert FAKE_PIN not in repr(settings.operator_pin)


def _revealed(settings: Settings) -> str | None:
    key = settings.effective_llm_api_key()
    return None if key is None else key.get_secret_value()
