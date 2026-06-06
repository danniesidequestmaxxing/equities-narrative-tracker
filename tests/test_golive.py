"""Go-live prep: paper-trade mode + preflight readiness."""

from datetime import datetime, timezone

from narrative_tracker import jobs
from narrative_tracker.analyze.analyzer import Analyzer
from narrative_tracker.config import Settings
from narrative_tracker.db import recs
from narrative_tracker.enrich.market_data import FakeMarketData, MarketSnapshot
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.preflight import check_db, check_readiness, is_go
from narrative_tracker.recommend.types import RiskConfig

NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)


def _bullish_analyzer() -> Analyzer:
    a = Analyzer()
    for i, acct in enumerate(["111", "222", "333", "111"]):
        a.ingest(symbol="NVDA", text="$NVDA strong breakout", stance="bullish",
                 stance_confidence=0.9, credibility=0.6, ts=NOW.timestamp() - 10 + i, account=acct)
    return a


def _market():
    return FakeMarketData({"NVDA": MarketSnapshot("NVDA", "equity", 150.0, 5e8, 0.05, 3e12, 4.0, NOW)})


async def test_paper_mode_tracks_without_broadcasting(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=1)
    res = await jobs.run_recommend(
        session_factory, _bullish_analyzer(), _market(), notifier, RiskConfig(),
        now=NOW, date_label="2026-06-03", paper=True,
    )
    assert res["paper"] == 1 and res["broadcast"] == 0
    assert fake_bot.sent == []                                    # NOT broadcast to the group
    assert "NVDA" in await recs.live_symbols(session_factory)     # but tracked -> will be scored


async def test_live_mode_broadcasts(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=1)
    res = await jobs.run_recommend(
        session_factory, _bullish_analyzer(), _market(), notifier, RiskConfig(),
        now=NOW, date_label="2026-06-03", paper=False,
    )
    assert res["broadcast"] == 1 and res["paper"] == 0
    assert any("CALL" in m["text"] for m in fake_bot.sent)


def test_preflight_nogo_without_required_keys():
    s = Settings(_env_file=None)  # defaults: no feed, no telegram
    assert is_go(check_readiness(s)) is False


def test_preflight_go_with_required_keys():
    s = Settings(_env_file=None, twitterapi_io_key="k", telegram_bot_token="t", telegram_trading_chat_id=123)
    assert is_go(check_readiness(s)) is True


def test_preflight_reports_mode():
    live = Settings(_env_file=None, paper_trade=False)
    mode = next(d for n, _, d in check_readiness(live) if n == "mode")
    assert "LIVE" in mode
    paper = Settings(_env_file=None, paper_trade=True)
    assert "PAPER" in next(d for n, _, d in check_readiness(paper) if n == "mode")


async def test_preflight_db_check_connects(db_url):
    s = Settings(_env_file=None, database_url=db_url)
    ok, detail = await check_db(s)
    assert ok is True and detail == "connected"
