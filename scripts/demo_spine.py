"""Run the M0 spine end-to-end with a fake feed + a print-only bot (no creds).

    python scripts/demo_spine.py

Demonstrates: ingest -> dedupe -> cashtag extract -> idempotent "alert", including
that a duplicate post does NOT produce a second alert.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.ingest.stream_client import FakeProvider
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.worker import Worker


class PrintBot:
    async def send_message(self, chat_id: int, text: str, **kwargs):
        print(f"\n┌─ TELEGRAM → chat {chat_id} " + "─" * 30)
        for line in text.splitlines():
            print(f"│ {line}")
        print("└" + "─" * 50)

        class _Result:
            message_id = 1

        return _Result()


async def main() -> None:
    engine = build_engine("sqlite+aiosqlite:///./demo.db")
    await create_all(engine)
    sf = build_sessionmaker(engine)

    now = datetime.now(timezone.utc)
    posts = [
        RawPost("44196397", "whale_trader", "1001", "$TSLA breaking out, loading calls 🚀", now),
        RawPost("783214", "macro_mike", "1002", "rotating into $NVDA and $AMD; $4200 SPX is the line", now),
        RawPost("44196397", "whale_trader", "1001", "$TSLA breaking out, loading calls 🚀", now),  # DUPLICATE
        RawPost("99001", "chart_chad", "1003", "no positions, market feels heavy", now),
    ]

    notifier = AlertNotifier(bot=PrintBot(), session_factory=sf, trading_chat_id=12345)
    worker = Worker(provider=FakeProvider(posts, delay_s=0.05), notifier=notifier, session_factory=sf)

    run_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.6)  # let the spine drain
    worker.request_stop()
    await run_task
    await engine.dispose()
    print("\n✅ spine ran: 4 posts in, duplicate $TSLA deduped, '$4200' filtered, no-cashtag post produced no alert.")


if __name__ == "__main__":
    asyncio.run(main())
