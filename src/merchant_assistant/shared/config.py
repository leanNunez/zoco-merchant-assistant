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

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SecretStr keeps the key out of reprs, logs and tracebacks.
    openai_api_key: SecretStr | None = None
    embeddings_model: str = "text-embedding-3-small"
    embeddings_cache_path: Path = Path("data/cache/embeddings.sqlite")
    business_timezone: str = "America/Argentina/Buenos_Aires"

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

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read once."""
    return Settings()
