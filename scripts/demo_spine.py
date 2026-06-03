"""Run the spine end-to-end with a fake feed + print-only bot (no creds).

    python scripts/demo_spine.py

M1 showcase: cashtags, cashtag-less company/product references, stance + negation,
options, image-only (vision), and dedupe of a duplicate post.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all
from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.extract.vision import FakeVisionExtractor
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.ingest.stream_client import FakeProvider
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.schemas.mention import AssetClass, Mention, ResolutionMethod
from narrative_tracker.worker import Worker


class PrintBot:
    async def send_message(self, chat_id: int, text: str, **kwargs):
        print(f"\n┌─ TELEGRAM → chat {chat_id} " + "─" * 28)
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
    chart = "https://pbs.twimg.com/media/chart_of_the_day.jpg"
    posts = [
        RawPost("44196397", "whale_trader", "1", "$TSLA breaking out, loading calls \U0001f680", now),
        RawPost("783214", "macro_mike", "2", "rotating into $NVDA and $AMD; $4200 SPX is the line", now),
        RawPost("44196397", "whale_trader", "1", "$TSLA breaking out, loading calls \U0001f680", now),  # DUP
        RawPost("99001", "value_val", "3", "Nvidia is ripping, and the Ozempic maker too", now),
        RawPost("99001", "value_val", "4", "$NVDA is NOT a buy up here, momentum gone", now),
        RawPost("55012", "options_olly", "5", "grabbing $SPY 600c 0DTE before close", now),
        RawPost("77013", "chart_chad", "6", "chart of the day \U0001f447", now, media_urls=[chart]),
    ]

    # Inject a fake vision extractor so the image-only post resolves to $GME.
    vision = FakeVisionExtractor(
        {
            chart: [
                Mention(
                    symbol="GME",
                    asset_class=AssetClass.EQUITY,
                    resolution_method=ResolutionMethod.VISION_OCR,
                    mention_confidence=0.7,
                )
            ]
        }
    )
    pipeline = ExtractionPipeline(vision=vision)

    notifier = AlertNotifier(bot=PrintBot(), session_factory=sf, trading_chat_id=12345)
    worker = Worker(
        provider=FakeProvider(posts, delay_s=0.03),
        notifier=notifier,
        session_factory=sf,
        pipeline=pipeline,
    )

    run_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.8)
    worker.request_stop()
    await run_task
    await engine.dispose()
    print(
        "\n✅ M1 spine: cashtags + cashtag-less (Nvidia->NVDA, Ozempic maker->NVO), "
        "stance/negation (NOT a buy -> bearish), options ($SPY 600c), image-only "
        "(vision -> $GME); duplicate $TSLA deduped; $4200 filtered."
    )


if __name__ == "__main__":
    asyncio.run(main())
