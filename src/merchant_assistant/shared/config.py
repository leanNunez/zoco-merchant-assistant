"""Settings read from the environment (and `.env`), validated when they are loaded.

A wrong value must stop the program at start-up with a message that says what to fix,
not surface later as a confusing provider error in the middle of a conversation. The API
key is optional on purpose: with a complete embeddings cache the evaluation runs offline,
so only the code that really needs the provider complains when the key is missing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from merchant_assistant.shared.faults import FaultConfig, FaultMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SecretStr keeps the key out of reprs, logs and tracebacks.
    openai_api_key: SecretStr | None = None
    embeddings_model: str = "text-embedding-3-small"
    embeddings_cache_path: Path = Path("data/cache/embeddings.sqlite")
    business_timezone: str = "America/Argentina/Buenos_Aires"

    # Simulated tool failures (brief, Part 2). Off by default: 0 never fails, 1 always fails.
    tool_failure_rate: float = 0.0
    tool_failure_seed: int = 0
    tool_failure_modes: str = "timeout,error,lost_ack"
    sales_path: Path = Path("data/ventas.json")
    # Runtime state, git-ignored. The committed data/reclamos.db is only the empty template.
    complaints_db_path: Path = Path("var/reclamos.db")

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_key_means_no_key(cls, value: object) -> object:
        # `.env.example` ships `OPENAI_API_KEY=`; copied as is, that is "no key", not a key
        # made of an empty string that the provider would reject with a 401.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("embeddings_model")
    @classmethod
    def _model_is_named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("EMBEDDINGS_MODEL must name a model, e.g. text-embedding-3-small")
        return value

    @field_validator("business_timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"BUSINESS_TIMEZONE '{value}' is not an IANA timezone; "
                "use a name such as America/Argentina/Buenos_Aires"
            ) from None
        return value

    @field_validator("tool_failure_rate")
    @classmethod
    def _rate_is_a_probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:  # written so that NaN is refused too
            raise ValueError(f"TOOL_FAILURE_RATE must be between 0 and 1, got {value}")
        return value

    @field_validator("tool_failure_modes")
    @classmethod
    def _modes_are_known(cls, value: str) -> str:
        _parse_modes(value)
        return value

    @model_validator(mode="after")
    def _failures_have_a_mode(self) -> Settings:
        if self.tool_failure_rate > 0 and not _parse_modes(self.tool_failure_modes):
            raise ValueError(
                "TOOL_FAILURE_MODES must list at least one mode when TOOL_FAILURE_RATE is above 0; "
                f"allowed: {_ALLOWED_MODES}"
            )
        return self

    def fault_config(self) -> FaultConfig:
        return FaultConfig(
            rate=self.tool_failure_rate,
            seed=self.tool_failure_seed,
            modes=_parse_modes(self.tool_failure_modes),
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)


_ALLOWED_MODES = ", ".join(mode.value for mode in FaultMode)


def _parse_modes(csv: str) -> frozenset[FaultMode]:
    names = [item.strip() for item in csv.split(",") if item.strip()]
    unknown = [name for name in names if name not in set(FaultMode)]
    if unknown:
        raise ValueError(
            f"TOOL_FAILURE_MODES has unknown mode(s) {', '.join(unknown)}; "
            f"allowed: {_ALLOWED_MODES}"
        )
    return frozenset(FaultMode(name) for name in names)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read once."""
    return Settings()
