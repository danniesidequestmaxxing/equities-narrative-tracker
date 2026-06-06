"""Runtime configuration.

All settings have stub-safe defaults so the package imports and the test suite
runs with zero credentials. Real values come from environment variables prefixed
with ``NT_`` (or a local ``.env``). See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings."""

    # Database — SQLite by default (local/tests); Postgres in prod.
    # Prod: postgresql+asyncpg://user:pass@host:5432/narrative_tracker
    database_url: str = "sqlite+aiosqlite:///./narrative_tracker.db"

    # Redis is an optional fast-path cache in M0; Postgres is authoritative (INV-1).
    redis_url: str | None = None

    # X / Twitter data feed (twitterapi.io).
    twitterapi_io_key: str | None = None
    twitterapi_io_ws_url: str = "wss://api.twitterapi.io/twitter/stream"

    # Telegram.
    telegram_bot_token: str | None = None
    telegram_trading_chat_id: int | None = None
    telegram_ops_chat_id: int | None = None

    # Market data (Polygon / "Massive"). Empty -> recommend + scoring stay disabled.
    polygon_api_key: str | None = None

    # LLM (stance / extraction). Empty -> deterministic rule-based fallback only.
    llm_model: str | None = None          # e.g. "openai/gpt-5.2", "anthropic/claude-opus-4-8"
    llm_budget_usd: float = 100.0

    # Observability.
    healthchecks_url: str | None = None

    # Worker behavior.
    alert_latency_budget_s: int = 60

    model_config = SettingsConfigDict(
        env_prefix="NT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_trading_chat_id)

    @property
    def feed_configured(self) -> bool:
        return bool(self.twitterapi_io_key)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
