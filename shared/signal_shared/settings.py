from __future__ import annotations

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_csv(value: str) -> list[str]:
    """Splits a comma-separated env var into trimmed, non-empty parts."""
    return [part.strip() for part in value.split(",") if part.strip()]


def describe_config_error(exc: ValidationError) -> str:
    """Summarize a settings ValidationError without leaking any field's value.

    pydantic's default error message includes an ``input_value`` dump of the
    *entire* input dict on every error, which would print other secrets (e.g.
    TG_API_HASH) any time a different field is missing or invalid.
    """
    parts = [f"{'.'.join(str(p) for p in err['loc'])} ({err['type']})" for err in exc.errors()]
    return "invalid configuration: " + "; ".join(parts)


class BaseAppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str = "redis://redis:6379/0"
    quote_assets: str = "USDT"
    log_level: str = "INFO"

    @property
    def quote_assets_list(self) -> list[str]:
        return [q.upper() for q in split_csv(self.quote_assets)]
