"""M6: cadence jobs (digest, recommend, scoring) + the full feedback loop."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from narrative_tracker import jobs
from narrative_tracker.analyze.analyzer import Analyzer
from narrative_tracker.db import recs, repo
from narrative_tracker.enrich.market_data import FakeMarketData, MarketSnapshot
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.ops import killswitch
from narrative_tracker.recommend.types import RiskConfig
from narrative_tracker.scorer.types import Bar
from narrative_tracker.worker import process_post
from narrative_tracker.ingest.provider import RawPost

NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)
DAY = 86400


def _bullish_nvda_analyzer(now_ts: float) -> Analyzer:
    a = Analyzer()
    for i, acct in enumerate(["111", "222", "333", "111", "222"]):
        a.ingest(
            symbol="NVDA", text="$NVDA ai gpu strong breakout", stance="bullish",
            stance_confidence=0.9, credibility=0.6, ts=now_ts - 60 + i, account=acct,
        )
    return a


def _nvda_snapshot(as_of):
    return MarketSnapshot("NVDA", "equity", 150.0, 5e8, 0.05, 3e12, 4.0, as_of)


def _notifier(fake_bot, sf):
    return AlertNotifier(bot=fake_bot, session_factory=sf, trading_chat_id=1)


async def test_run_digest_broadcasts(session_factory, fake_bot):
    a = _bullish_nvda_analyzer(NOW.timestamp())
    res = await jobs.run_digest(
        session_factory, a, _notifier(fake_bot, session_factory),
        cadence_label="Daily", date_label="2026-06-03", now_ts=NOW.timestamp(),
    )
    assert res["broadcast"] is True
    assert len(fake_bot.sent) == 1 and "Digest" in fake_bot.sent[0]["text"]


async def test_run_recommend_persists_and_broadcasts(session_factory, fake_bot):
    a = _bullish_nvda_analyzer(NOW.timestamp())
    market = FakeMarketData({"NVDA": _nvda_snapshot(NOW)})
    res = await jobs.run_recommend(
        session_factory, a, market, _notifier(fake_bot, session_factory), RiskConfig(),
        now=NOW, date_label="2026-06-03",
    )
    assert res["calls"] == 1 and res["broadcast"] == 1
    assert any("CALL" in m["text"] for m in fake_bot.sent)
    assert "NVDA" in await recs.live_symbols(session_factory)  # persisted + live


async def test_run_recommend_paused_holds_call(session_factory, fake_bot):
    await killswitch.set_pause(session_factory, killswitch.PAUSE_BROADCAST)
    a = _bullish_nvda_analyzer(NOW.timestamp())
    market = FakeMarketData({"NVDA": _nvda_snapshot(NOW)})
    res = await jobs.run_recommend(
        session_factory, a, market, _notifier(fake_bot, session_factory), RiskConfig(),
        now=NOW, date_label="2026-06-03",
    )
    assert res["calls"] == 1 and res["broadcast"] == 0  # generated but held
    assert fake_bot.sent == []
    assert "NVDA" not in await recs.live_symbols(session_factory)  # not marked live


async def test_run_recommend_killed_skips(session_factory, fake_bot):
    await killswitch.engage_killswitch(session_factory)
    res = await jobs.run_recommend(
        session_factory, Analyzer(), FakeMarketData({}), _notifier(fake_bot, session_factory),
        RiskConfig(), now=NOW, date_label="2026-06-03",
    )
    assert res.get("skipped") == "killed"


async def test_full_loop_recommend_then_score_updates_credibility(session_factory, fake_bot):
    # accounts must exist so credibility can persist back to them
    aid = await repo.get_or_create_account(session_factory, platform_user_id="111", handle="whale", tier="HOT")
    await repo.get_or_create_account(session_factory, platform_user_id="222", handle="mike", tier="WARM")
    await repo.get_or_create_account(session_factory, platform_user_id="333", handle="val", tier="COLD")

    issued = NOW - timedelta(days=20)
    a = _bullish_nvda_analyzer(issued.timestamp())
    await jobs.run_recommend(
        session_factory, a, FakeMarketData({"NVDA": _nvda_snapshot(issued)}),
        _notifier(fake_bot, session_factory), RiskConfig(), now=issued, date_label="2026-05-14",
    )
    assert "NVDA" in await recs.live_symbols(session_factory)

    async def bars_provider(symbol):
        t0 = int(issued.timestamp())
        bars = [Bar(t0 + n * DAY, D("150"), D("151"), D("149"), D("150")) for n in range(0, 8)]
        bars[6] = Bar(t0 + 6 * DAY, D("160"), D("170"), D("159"), D("169"))  # hits target ~162
        return bars

    res = await jobs.run_scoring(session_factory, bars_provider, now=NOW, max_age_s=10 * DAY)
    assert res["closed"] == 1 and res["credibility_updated"] >= 1
    assert "NVDA" not in await recs.live_symbols(session_factory)  # now closed
    assert await repo.get_credibility(session_factory, account_id=aid, as_of=NOW) > 0


async def test_process_post_feeds_analyzer(session_factory, fake_bot):
    a = Analyzer()
    post = RawPost("111", "whale", "p1", "$NVDA strong breakout", datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc))
    await process_post(post, session_factory=session_factory, notifier=_notifier(fake_bot, session_factory), analyzer=a)
    assert "NVDA" in a.sentiment.symbols()
    assert a.contributors_for("NVDA")  # contributor tracked for attribution
