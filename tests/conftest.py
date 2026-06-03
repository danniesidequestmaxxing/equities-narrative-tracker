"""Shared test fixtures.

Tests run against a temp-file SQLite database (so a fresh sessionmaker can prove
that idempotency survives a worker "restart") and a fake Telegram bot that simply
records what it would have sent. Zero credentials, zero network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all
from narrative_tracker.ingest.provider import RawPost


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/test.db"


@pytest_asyncio.fixture
async def session_factory(db_url):
    engine = build_engine(db_url)
    await create_all(engine)
    sf = build_sessionmaker(engine)
    yield sf
    await engine.dispose()


class FakeBot:
    """Records messages instead of sending them. ``message_id`` increments."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})

        class _Result:
            message_id = len(self.sent)

        return _Result()


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def make_post():
    """Factory fixture for building RawPost objects in tests."""

    def _make(
        *,
        text: str = "$AAPL ripping",
        post_id: str = "p1",
        user_id: str = "u1",
        handle: str = "trader",
    ) -> RawPost:
        return RawPost(
            platform_user_id=user_id,
            handle=handle,
            platform_post_id=post_id,
            text=text,
            posted_at=datetime(2026, 6, 3, 14, 23, tzinfo=timezone.utc),
        )

    return _make
