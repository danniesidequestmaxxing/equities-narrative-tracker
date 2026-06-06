"""The M0 pipeline spine.

    provider.stream()  ->  buffer  ->  process_post  ->  idempotent Telegram alert

``process_post`` is the testable unit. Idempotency is layered:

1. ``insert_post_if_new`` dedupes the post record (and gates one-time work like
   adding mention rows + the heartbeat).
2. The notifier's ``claim_send`` dedupes each alert. Alerts are attempted on
   *every* sighting of a post (cheap re-extraction), so a crash between the post
   insert and the send is recovered on restart — while a fully-processed post
   never double-posts.

This realizes the plan's "missed alert beats a duplicate" posture without missing
alerts in the common crash window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .analyze.analyzer import Analyzer
from .db import idempotency, repo
from .extract.pipeline import ExtractionPipeline
from .ingest.buffer import IngestBuffer
from .ingest.provider import RawPost, SourceProvider
from .notify.telegram_bot import AlertNotifier
from .ops.heartbeat import ping_heartbeat
from .scheduler import Scheduler

log = logging.getLogger(__name__)

# Default M1 pipeline (rule-based stance, no vision). Prod injects LLM + vision.
_DEFAULT_PIPELINE = ExtractionPipeline()


async def process_post(
    post: RawPost,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    notifier: AlertNotifier,
    pipeline: ExtractionPipeline | None = None,
    analyzer: Analyzer | None = None,
    heartbeat_url: str | None = None,
    min_confidence: float = 0.0,
) -> dict:
    """Process one post end-to-end. Returns a small result dict for tests/audit."""
    pipeline = pipeline or _DEFAULT_PIPELINE
    account_id = await repo.get_or_create_account(
        session_factory,
        platform_user_id=post.platform_user_id,
        handle=post.handle,
    )
    post_id, is_new = await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id=post.platform_post_id,
        text=post.text,
        posted_at=post.posted_at,
        post_type=post.post_type,
    )

    # Extraction is deterministic -> always run so a recovery can still alert.
    # Persisting mentions is one-time work, gated on is_new.
    mentions = await pipeline.extract(
        text=post.text,
        media_urls=post.media_urls,
        source_post_id=post.platform_post_id,
    )
    if is_new:
        await ping_heartbeat(heartbeat_url)  # heartbeat on row-written
        await repo.add_mentions(
            session_factory, post_id=post_id, mentions=[m.to_row() for m in mentions]
        )
        await repo.set_post_state(
            session_factory,
            post_id=post_id,
            state="live" if mentions else "archived",
        )
        # Feed the standing Analyzer (sentiment + narratives + contributors) so the
        # cadence jobs have state to work from. Credibility is the as-of value.
        if analyzer is not None and mentions:
            cred = await repo.get_credibility(
                session_factory, account_id=account_id, as_of=post.posted_at
            )
            for m in mentions:
                analyzer.ingest(
                    symbol=m.symbol, text=post.text, stance=m.stance.value,
                    stance_confidence=m.stance_confidence, credibility=cred,
                    ts=post.posted_at.timestamp(), account=post.platform_user_id,
                    asset_class=m.asset_class.value,
                )

    alerts_sent = 0
    for mention in mentions:
        if mention.mention_confidence < min_confidence:
            continue
        if await notifier.send_alert(post, mention):
            alerts_sent += 1

    if is_new:
        await repo.record_audit(
            session_factory,
            event_type="post_processed",
            payload={
                "post_id": post_id,
                "symbols": [m.symbol for m in mentions],
                "alerts_sent": alerts_sent,
            },
        )

    return {
        "post_id": post_id,
        "deduped": not is_new,
        "symbols": [m.symbol for m in mentions],
        "alerts_sent": alerts_sent,
    }


class Worker:
    """Runs the ingest + process loops with graceful shutdown."""

    def __init__(
        self,
        *,
        provider: SourceProvider,
        notifier: AlertNotifier,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: ExtractionPipeline | None = None,
        analyzer: Analyzer | None = None,
        scheduler: Scheduler | None = None,
        heartbeat_url: str | None = None,
        min_confidence: float = 0.0,
        tick_interval_s: float = 30.0,
        buffer_maxsize: int = 1000,
    ) -> None:
        self._provider = provider
        self._notifier = notifier
        self._sf = session_factory
        self._pipeline = pipeline or _DEFAULT_PIPELINE
        self._analyzer = analyzer
        self._scheduler = scheduler
        self._heartbeat_url = heartbeat_url
        self._min_confidence = min_confidence
        self._tick_interval_s = tick_interval_s
        self._buffer = IngestBuffer(maxsize=buffer_maxsize)
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Signal the worker to drain and exit (also wired to SIGINT/SIGTERM)."""
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._process_loop(), name="process"),
        ]
        if self._scheduler is not None:
            tasks.append(asyncio.create_task(self._scheduler_loop(), name="scheduler"))
        await self._stop.wait()
        log.info("shutdown signal received; draining")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self._scheduler.tick(datetime.now(timezone.utc))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval_s)

    async def _ingest_loop(self) -> None:
        # Ingest only writes to the buffer; processing happens downstream so the
        # stream is never blocked (INV-6).
        async for post in self._provider.stream():
            if self._stop.is_set():
                break
            await self._buffer.put(post)

    async def _process_loop(self) -> None:
        while not self._stop.is_set():
            try:
                post = await asyncio.wait_for(self._buffer.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await process_post(
                    post,
                    session_factory=self._sf,
                    notifier=self._notifier,
                    pipeline=self._pipeline,
                    analyzer=self._analyzer,
                    heartbeat_url=self._heartbeat_url,
                    min_confidence=self._min_confidence,
                )
            except Exception:  # noqa: BLE001 - one bad post must not kill the loop
                log.exception("failed to process post %s", post.platform_post_id)
            finally:
                self._buffer.task_done()


async def main() -> None:  # pragma: no cover - prod entrypoint
    """Production entrypoint: build real provider + bot from settings and run."""
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO)
    from .config import get_settings
    from .db.base import build_engine, build_sessionmaker, create_all
    from .ingest.stream_client import TwitterApiIoStreamClient
    from .notify.telegram_bot import build_aiogram_bot

    settings = get_settings()
    if not settings.feed_configured or not settings.telegram_configured:
        log.error(
            "Missing credentials. Set NT_TWITTERAPI_IO_KEY, NT_TELEGRAM_BOT_TOKEN, "
            "NT_TELEGRAM_TRADING_CHAT_ID (see .env.example). Nothing to run yet."
        )
        return

    engine = build_engine(settings.database_url)
    await create_all(engine)
    session_factory = build_sessionmaker(engine)

    # Watchlist wiring (account ids) is loaded from the DB in a later milestone;
    # M0 reads them from settings/env once provided.
    provider = TwitterApiIoStreamClient(
        api_key=settings.twitterapi_io_key,
        ws_url=settings.twitterapi_io_ws_url,
        account_ids=[],
    )
    bot = build_aiogram_bot(settings.telegram_bot_token)  # type: ignore[arg-type]
    notifier = AlertNotifier(
        bot=bot,
        session_factory=session_factory,
        trading_chat_id=settings.telegram_trading_chat_id,  # type: ignore[arg-type]
    )
    # LLM stance when configured; deterministic rule-based otherwise (fail-safe).
    from . import jobs as cadence
    from .analyze.analyzer import Analyzer
    from .extract.stance import build_stance_classifier
    from .recommend.types import RiskConfig
    from .scheduler import ScheduledJob, Scheduler

    pipeline = ExtractionPipeline(stance=build_stance_classifier(model=settings.llm_model))
    analyzer = Analyzer()

    # Digest needs no market data, so it always runs. Recommend + scoring require a
    # real MarketDataProvider (Polygon) + bars feed — wired at go-live; until then
    # the service runs alerts + analyzer + digests.
    scheduled = [
        ScheduledJob(
            "digest-daily", 86400.0,
            lambda now: cadence.run_digest(
                session_factory, analyzer, notifier,
                cadence_label="Daily", date_label=now.strftime("%Y-%m-%d"),
                now_ts=now.timestamp(),
            ),
        )
    ]
    market_provider = None  # TODO(go-live): PolygonMarketData(settings...)
    if market_provider is not None:  # pragma: no cover
        config = RiskConfig()
        scheduled.append(ScheduledJob(
            "recommend", 3600.0,
            lambda now: cadence.run_recommend(
                session_factory, analyzer, market_provider, notifier, config,
                now=now, date_label=now.strftime("%Y-%m-%d"),
            ),
        ))
    else:
        log.warning("market data provider not configured; recommend + scoring disabled")

    worker = Worker(
        provider=provider,
        notifier=notifier,
        session_factory=session_factory,
        pipeline=pipeline,
        analyzer=analyzer,
        scheduler=Scheduler(scheduled),
        heartbeat_url=settings.healthchecks_url,
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
