"""Seed a local SQLite DB with sample mentions so the dashboard has data to show.

    NT_DATABASE_URL=sqlite+aiosqlite:///./dash_demo.db python scripts/seed_dashboard.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NT_DATABASE_URL", "sqlite+aiosqlite:///./dash_demo.db")

from narrative_tracker.config import get_settings  # noqa: E402
from narrative_tracker.db import idempotency, repo  # noqa: E402
from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all  # noqa: E402

NOW = datetime.now(timezone.utc)

ACCOUNTS = [
    ("blknoiz06", "HOT"), ("CryptoCred", "HOT"), ("macro_mike", "WARM"),
    ("value_val", "WARM"), ("chart_chad", "COLD"), ("degen_dan", "COLD"),
]
# (handle, post_id, minutes_ago, text, [(symbol, stance, sconf)])
POSTS = [
    ("blknoiz06", "p1", 8, "$NVDA breaking out, loading calls — ai gpu demand insane", [("NVDA", "bullish", 0.9)]),
    ("macro_mike", "p2", 14, "still long $NVDA here, data center buildout just starting", [("NVDA", "bullish", 0.75)]),
    ("value_val", "p3", 22, "Nvidia momentum strong, $MRVL ripping too", [("NVDA", "bullish", 0.7), ("MRVL", "bullish", 0.7)]),
    ("blknoiz06", "p4", 35, "$MRVL up 50% since Jensen called it the next trillion $ stock", [("MRVL", "bullish", 0.85)]),
    ("CryptoCred", "p5", 18, "$BTC reclaiming the range, bid is back", [("BTC", "bullish", 0.8)]),
    ("degen_dan", "p6", 12, "aped $BB calls, robotics infra QNX thesis", [("BB", "bullish", 0.9)]),
    ("chart_chad", "p7", 40, "$TSLA looks heavy here, fading the pop", [("TSLA", "bearish", 0.7)]),
    ("macro_mike", "p8", 55, "$TSLA not a buy up here, momentum gone", [("TSLA", "bearish", 0.8)]),
    ("value_val", "p9", 70, "$AMD lagging $NVDA badly, avoid", [("AMD", "bearish", 0.65), ("NVDA", "bullish", 0.6)]),
    ("CryptoCred", "p10", 90, "$ETH finally waking up, $SOL leading", [("ETH", "bullish", 0.75), ("SOL", "bullish", 0.8)]),
    ("blknoiz06", "p11", 5, "adding more $NVDA, this is the trade of the year", [("NVDA", "bullish", 0.95)]),
    ("degen_dan", "p12", 28, "$BB sending, QNX + nvidia partnership huge", [("BB", "bullish", 0.85)]),
]


async def main() -> None:
    engine = build_engine(get_settings().database_url)
    await create_all(engine)
    sf = build_sessionmaker(engine)

    ids = {}
    for handle, tier in ACCOUNTS:
        ids[handle] = await repo.get_or_create_account(sf, platform_user_id=handle, handle=handle, tier=tier)

    for handle, pid, mins, text, mentions in POSTS:
        post_id, _ = await idempotency.insert_post_if_new(
            sf, account_id=ids[handle], platform_post_id=pid, text=text,
            posted_at=NOW - timedelta(minutes=mins),
        )
        await repo.add_mentions(sf, post_id=post_id, mentions=[
            {"symbol": s, "asset_class": "crypto" if s in {"BTC", "ETH", "SOL"} else "equity",
             "stance": st, "stance_confidence": sc, "mention_confidence": 0.95}
            for s, st, sc in mentions
        ])
    await engine.dispose()
    print("seeded dash_demo.db")


if __name__ == "__main__":
    asyncio.run(main())
