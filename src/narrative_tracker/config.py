"""Runtime configuration.

All settings have stub-safe defaults so the package imports and the test suite
runs with zero credentials. Real values come from environment variables prefixed
with ``NT_`` (or a local ``.env``). See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings."""

    # Database — SQLite by default (local/tests); Postgres in prod.
    # Prod: postgresql+asyncpg://user:pass@host:5432/narrative_tracker
    database_url: str = "sqlite+aiosqlite:///./narrative_tracker.db"

    # Redis is an optional fast-path cache in M0; Postgres is authoritative (INV-1).
    redis_url: str | None = None

    # X / Twitter data feed (twitterapi.io) — polling via advanced_search.
    twitterapi_io_key: str | None = None
    twitterapi_io_base_url: str = "https://api.twitterapi.io"
    watchlist: str = ""          # comma-separated handles to poll, e.g. "blknoiz06,elonmusk"
    poll_interval_s: int = 120
    initial_lookback_s: int = 3600  # on first poll, pull the last hour so it's not silent

    # Telegram.
    telegram_bot_token: str | None = None
    telegram_trading_chat_id: int | None = None
    telegram_ops_chat_id: int | None = None
    admin_ids: str = ""  # comma-separated Telegram user ids allowed to run /admin commands

    # Market data (Massive, formerly Polygon.io — keys work on both domains).
    # Empty -> recommend + scoring + pulse TA stay disabled.
    polygon_api_key: str | None = None
    polygon_base_url: str = "https://api.massive.com"  # legacy api.polygon.io also works

    # Paper-trade mode: generate + score calls but DO NOT broadcast to the group.
    # Default ON — flip to false only after a paper-trading period proves accuracy.
    paper_trade: bool = True

    # Pulse: the recurring investor briefing on Telegram (account recap, hot
    # tickers, early radar, TA + fundamentals, narratives). 0 -> disabled.
    pulse_interval_hours: float = 8.0

    # M15 conviction routing: alerts below `silent` arrive without a phone
    # buzz (still in the channel + ledger); below `min` no alert is sent at
    # all (mention still recorded and graded). 0 disables each behavior.
    alert_silent_below_conviction: float = 0.5
    alert_min_conviction: float = 0.0

    # LLM (stance / extraction). Empty -> deterministic rule-based fallback only.
    llm_model: str | None = None          # e.g. "openai/gpt-5.2", "anthropic/claude-opus-4-8"
    llm_budget_usd: float = 100.0
    # Hard daily cap on LLM calls across all surfaces (cost guardrail for the
    # public watchlist). Hitting it degrades to rule-based until UTC midnight.
    # 0 = uncapped.
    llm_daily_call_cap: int = 2000

    # Public dashboard can add accounts; cap the list so strangers can't run up
    # the polling + LLM bill. Owner adds via the bot bypass the cap.
    max_watchlist: int = 25

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

    @field_validator("database_url")
    @classmethod
    def _ensure_async_pg(cls, v: str) -> str:
        """Accept Railway/Heroku-style ``postgres(ql)://`` URLs and rewrite them to
        the async driver the app needs — so a plain ``${{Postgres.DATABASE_URL}}``
        reference works without manual scheme surgery."""
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_trading_chat_id)

    @property
    def feed_configured(self) -> bool:
        return bool(self.twitterapi_io_key)

    @property
    def watchlist_handles(self) -> list[str]:
        return [h.strip().lstrip("@") for h in self.watchlist.split(",") if h.strip()]

    @property
    def admin_id_list(self) -> list[int]:
        out = []
        for part in self.admin_ids.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
