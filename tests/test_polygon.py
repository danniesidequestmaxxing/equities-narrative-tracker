"""M7: Polygon provider parsing + market_bars ingestion + DB-backed scoring."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from narrative_tracker import jobs
from narrative_tracker.analyze.analyzer import Analyzer
from narrative_tracker.db import bars as db_bars
from narrative_tracker.db import recs, repo
from narrative_tracker.db.bars import DbBarsProvider, DbLedgerProvider
from narrative_tracker.enrich.market_data import FakeMarketData, MarketSnapshot
from narrative_tracker.enrich.polygon import PolygonMarketData
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.recommend.types import RiskConfig
from narrative_tracker.scorer.types import AdjKind

NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)
DAY = 86400


def _make_fetch(responses: dict):
    async def fetch(path: str, params: dict) -> dict:
        for prefix, data in responses.items():
            if path.startswith(prefix):
                return data
        return {}

    return fetch


def _aggs(n=25, base=150.0):
    t0 = 1_700_000_000_000  # ms
    return {
        "results": [
            {"t": t0 + i * 86400000, "o": base + i * 0.5 - 1, "h": base + i * 0.5 + 1,
             "l": base + i * 0.5 - 2, "c": base + i * 0.5, "v": 1_000_000}
            for i in range(n)
        ]
    }


async def test_polygon_snapshot_parses():
    fetch = _make_fetch({
        "/v2/aggs/ticker/NVDA": _aggs(),
        "/v3/reference/tickers/NVDA": {"results": {"market_cap": 3.6e12}},
    })
    p = PolygonMarketData(fetch=fetch, now=lambda: NOW)
    snap = await p.snapshot("NVDA", "equity")
    assert snap is not None
    assert snap.price == pytest.approx(150 + 24 * 0.5)  # last close
    assert snap.market_cap == 3.6e12
    assert snap.atr and snap.atr > 0 and snap.adv_usd > 0
    assert snap.session_open is True  # Wed 15:00 UTC


async def test_polygon_fetch_bars_unadjusted():
    p = PolygonMarketData(fetch=_make_fetch({"/v2/aggs/ticker/NVDA": _aggs(3)}), now=lambda: NOW)
    bars = await p.fetch_bars("NVDA")
    assert len(bars) == 3
    assert bars[0]["ts"] == 1_700_000_000  # ms -> s


async def test_polygon_fetch_adjustments():
    p = PolygonMarketData(fetch=_make_fetch({
        "/v3/reference/splits": {"results": [{"execution_date": "2026-03-01", "split_from": 1, "split_to": 2}]},
        "/v3/reference/dividends": {"results": [{"ex_dividend_date": "2026-02-01", "cash_amount": 0.5}]},
    }))
    adj = await p.fetch_adjustments("NVDA")
    assert {a["kind"] for a in adj} == {"split", "dividend"}
    assert next(a for a in adj if a["kind"] == "split")["value"] == 2.0


async def test_bars_save_load_idempotent(session_factory):
    rows = [{"ts": 1_700_000_000 + i * DAY, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i, "v": 1e6} for i in range(5)]
    assert await db_bars.save_bars(session_factory, symbol="NVDA", interval="1d", source="polygon", bars=rows) == 5
    assert await db_bars.save_bars(session_factory, symbol="NVDA", interval="1d", source="polygon", bars=rows) == 0
    loaded = await db_bars.load_bars(session_factory, symbol="NVDA")
    assert len(loaded) == 5 and loaded[0].close == D("100.5")


async def test_adjustments_roundtrip(session_factory):
    adjs = [{"ex_ts": 1_700_000_000, "kind": "split", "value": 2.0}, {"ex_ts": 1_700_100_000, "kind": "dividend", "value": 0.5}]
    await db_bars.save_adjustments(session_factory, symbol="NVDA", source="polygon", adjustments=adjs)
    led = await db_bars.load_ledger(session_factory, symbol="NVDA")
    assert len(led) == 2
    assert any(a.kind is AdjKind.SPLIT and a.value == D("2.0") for a in led)


async def test_scoring_off_db_bars(session_factory, fake_bot):
    """The whole point of M7: scoring reads stored bars, not an inline fake."""
    aid = await repo.get_or_create_account(session_factory, platform_user_id="111", handle="whale", tier="HOT")
    issued = NOW - timedelta(days=20)

    analyzer = Analyzer()
    for i in range(4):
        analyzer.ingest(symbol="NVDA", text="$NVDA strong breakout", stance="bullish",
                        stance_confidence=0.9, credibility=0.6, ts=issued.timestamp() - 10 + i, account="111")
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=1)
    await jobs.run_recommend(
        session_factory, analyzer, FakeMarketData({"NVDA": MarketSnapshot("NVDA", "equity", 150.0, 5e8, 0.05, 3e12, 4.0, issued)}),
        notifier, RiskConfig(), now=issued, date_label="2026-05-14",
    )
    assert "NVDA" in await recs.live_symbols(session_factory)

    # Ingest bars INTO THE DB; scoring reads them via DbBarsProvider.
    t0 = int(issued.timestamp())
    rows = [{"ts": t0 + n * DAY, "o": 150, "h": 151, "l": 149, "c": 150, "v": 1e6} for n in range(8)]
    rows[6] = {"ts": t0 + 6 * DAY, "o": 160, "h": 170, "l": 159, "c": 169, "v": 1e6}  # hits target ~162
    await db_bars.save_bars(session_factory, symbol="NVDA", interval="1d", source="polygon", bars=rows)

    res = await jobs.run_scoring(
        session_factory, DbBarsProvider(session_factory), now=NOW, max_age_s=10 * DAY,
        ledger_provider=DbLedgerProvider(session_factory),
    )
    assert res["closed"] == 1 and res["credibility_updated"] >= 1
    assert await repo.get_credibility(session_factory, account_id=aid, as_of=NOW) > 0
