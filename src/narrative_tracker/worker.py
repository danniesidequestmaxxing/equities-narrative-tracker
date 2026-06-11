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
from .db import calls as db_calls
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


async def _noop() -> None:
    return None


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

    if is_new and post.metrics:
        # M10: at-ingest engagement snapshot — raw tape for crowdedness analytics.
        await repo.save_engagement(session_factory, post_id=post_id, metrics=post.metrics)

    alerts_sent = 0
    for mention in mentions:
        if mention.mention_confidence < min_confidence:
            continue
        if await notifier.send_alert(post, mention):
            alerts_sent += 1

    # M10: reversal detection — the account flipped direction on a name, or is
    # talking against their own open stated call. Runs on every sighting (the
    # send is idempotent), matching the alert recovery posture.
    for mention in mentions:
        if mention.stance.value not in ("bullish", "bearish") or mention.stance_confidence < 0.6:
            continue
        prior = await repo.last_stance(
            session_factory, account_id=account_id, symbol=mention.symbol, before=post.posted_at
        )
        open_call = await db_calls.open_call_for(
            session_factory, account_id=account_id, symbol=mention.symbol
        )
        flipped = (
            prior is not None
            and prior["stance"] != mention.stance.value
            and (prior["stance_confidence"] or 0) >= 0.6
        )
        against_call = open_call is not None and (
            (open_call["direction"] == "long") == (mention.stance.value == "bearish")
        )
        if not flipped and not against_call:
            continue
        use_prior = prior if flipped else {
            "stance": "bullish" if open_call["direction"] == "long" else "bearish",
            "posted_at": open_call["stated_at"],
            "stance_confidence": 1.0,
        }
        await notifier.send_reversal(post, mention, use_prior, open_call if against_call else None)

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
    from .ingest.polling_client import TwitterApiIoPollingProvider
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

    # Seed NT_WATCHLIST into the DB (idempotent). After this the watchlist is
    # managed live via /addsource; the poller reads active handles each cycle.
    for handle in settings.watchlist_handles:
        await repo.get_or_create_account(
            session_factory, platform_user_id=handle.lower(), handle=handle, tier="COLD"
        )

    provider = TwitterApiIoPollingProvider(
        api_key=settings.twitterapi_io_key,
        handles_provider=lambda: repo.active_handles(session_factory),
        base_url=settings.twitterapi_io_base_url,
        poll_interval_s=settings.poll_interval_s,
        initial_lookback_s=settings.initial_lookback_s,
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

    from .extract.relevance import build_relevance_gate
    from .extract.vision import CachingBudgetedVision, CountBudget, build_llm_vision

    # M11: read chart/position screenshots on image-only posts. Cached per
    # media URL; budgeted per process lifetime so a retweet storm can't run
    # up the vision bill.
    vision = build_llm_vision(settings.llm_model)
    if vision is not None:
        vision = CachingBudgetedVision(vision, budget=CountBudget(500))

    pipeline = ExtractionPipeline(
        stance=build_stance_classifier(model=settings.llm_model),
        relevance=build_relevance_gate(model=settings.llm_model),
        vision=vision,
    )
    analyzer = Analyzer()

    # Digest needs no market data, so it always runs. Recommend + scoring require a
    # real MarketDataProvider (Polygon) + bars feed — wired at go-live; until then
    # the service runs alerts + analyzer + digests.
    market = None  # set below when Polygon is configured; the pulse degrades without it
    scheduled = [
        ScheduledJob(
            "digest-daily", 86400.0,
            lambda now: cadence.run_digest(
                session_factory, analyzer, notifier,
                cadence_label="Daily", date_label=now.strftime("%Y-%m-%d"),
                now_ts=now.timestamp(),
            ),
        ),
        # M11: weekly report, anchored to the Friday US close (Fri 21:00 UTC
        # = Sat 05:00 MYT). Checked every 10 min; the window + per-week
        # idempotency make it fire exactly once, even across restarts.
        ScheduledJob(
            "weekly-report", 600.0,
            lambda now: (
                cadence.run_weekly_report(session_factory, notifier, now=now)
                if cadence.weekly_report_due(now)
                else _noop()
            ),
        ),
    ]
    if settings.polygon_api_key:  # pragma: no cover
        from .db import recs
        from .db.bars import DbBarsProvider, DbLedgerProvider
        from .enrich.polygon import PolygonMarketData, build_polygon_fetch

        market = PolygonMarketData(
            fetch=build_polygon_fetch(settings.polygon_api_key, base_url=settings.polygon_base_url)
        )
        config = RiskConfig()
        bars_provider = DbBarsProvider(session_factory)
        ledger_provider = DbLedgerProvider(session_factory)

        scheduled.append(ScheduledJob(
            "recommend", 3600.0,
            lambda now: cadence.run_recommend(
                session_factory, analyzer, market, notifier, config,
                now=now, date_label=now.strftime("%Y-%m-%d"), paper=settings.paper_trade,
            ),
        ))
        log.info("recommend mode: %s", "PAPER (no broadcast)" if settings.paper_trade else "LIVE")

        async def _refresh_and_score(now):
            live = await recs.live_symbols(session_factory)
            await cadence.run_refresh_bars(session_factory, market, live)
            await cadence.run_scoring(
                session_factory, bars_provider, now=now, max_age_s=15 * 86400,
                ledger_provider=ledger_provider,
            )

        scheduled.append(ScheduledJob("refresh-score", 86400.0, _refresh_and_score))

        # M9 event-study: forward returns after every mention (backfills history
        # on first run; re-completes partial rows each cycle). Throttled for the
        # free Massive tier (~5 req/min).
        scheduled.append(ScheduledJob(
            "outcomes", 6 * 3600.0,
            lambda now: cadence.run_outcomes(session_factory, market, now=now, throttle_s=13.0),
        ))
    else:
        log.warning("market data provider not configured; recommend + scoring disabled")

    # M9-C: rolling explicit-call scan — backfills history, then keeps up with
    # new posts. Needs the LLM; 🎯 notifications go through the notifier.
    if settings.llm_model:  # pragma: no cover
        from .extract.calls_llm import build_call_extractor

        call_extractor = build_call_extractor(settings.llm_model)
        scheduled.append(ScheduledJob(
            "call-scan", 300.0,
            lambda now: cadence.run_call_scan(session_factory, call_extractor, notifier),
        ))
        log.info("explicit-call scan enabled (every 5 min)")

    # The recurring investor briefing (every NT_PULSE_INTERVAL_HOURS; 0 disables).
    # Degrades gracefully: no Polygon -> no TA section; no LLM -> seed-theme narratives.
    if settings.pulse_interval_hours > 0:  # pragma: no cover
        from .analyze.pulse import build_pulse_writer

        pulse_writer = build_pulse_writer(settings.llm_model)
        scheduled.append(ScheduledJob(
            "pulse", settings.pulse_interval_hours * 3600.0,
            lambda now: cadence.run_pulse(
                session_factory, notifier, now=now, hours=settings.pulse_interval_hours,
                market=market, writer=pulse_writer,
                watchlist_provider=lambda: repo.active_handles(session_factory),
            ),
        ))
        log.info("pulse briefing every %sh (llm=%s, market=%s)",
                 settings.pulse_interval_hours, bool(pulse_writer), market is not None)

    worker = Worker(
        provider=provider,
        notifier=notifier,
        session_factory=session_factory,
        pipeline=pipeline,
        analyzer=analyzer,
        scheduler=Scheduler(scheduled),
        heartbeat_url=settings.healthchecks_url,
    )

    # Live admin-command listener (/addsource etc.) alongside the worker.
    from .admin.bot import run_admin_bot

    admin_task = asyncio.create_task(
        run_admin_bot(bot, session_factory, settings.admin_id_list, market=market)
    )
    try:
        await worker.run()
    finally:
        admin_task.cancel()
        with contextlib.suppress(Exception):
            await admin_task


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
