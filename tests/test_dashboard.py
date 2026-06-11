"""Dashboard read-side analytics: ticker sentiment + hot tickers."""

from datetime import datetime, timedelta, timezone

from narrative_tracker.db import analytics, idempotency, repo

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


async def _seed(sf):
    whale = await repo.get_or_create_account(sf, platform_user_id="whale", handle="whale", tier="HOT")
    mike = await repo.get_or_create_account(sf, platform_user_id="mike", handle="mike", tier="WARM")
    p1, _ = await idempotency.insert_post_if_new(
        sf, account_id=whale, platform_post_id="1", text="$NVDA breaking out", posted_at=NOW - timedelta(minutes=10)
    )
    await repo.add_mentions(sf, post_id=p1, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.9, "mention_confidence": 0.97}])
    p2, _ = await idempotency.insert_post_if_new(
        sf, account_id=mike, platform_post_id="2", text="$NVDA up, $AMD looks weak", posted_at=NOW - timedelta(minutes=5)
    )
    await repo.add_mentions(sf, post_id=p2, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.8},
        {"symbol": "AMD", "asset_class": "equity", "stance": "bearish", "stance_confidence": 0.7}])


async def test_ticker_detail_aggregates_and_links(session_factory):
    await _seed(session_factory)
    d = await analytics.ticker_detail(session_factory, symbol="NVDA", since=SINCE)
    assert d["mentions"] == 2 and d["sentiment"] > 0  # two bullish takes
    assert {t["handle"] for t in d["takes"]} == {"whale", "mike"}
    assert any("x.com/whale/status/1" in t["url"] for t in d["takes"])
    assert any(t["tier"] == "HOT" for t in d["takes"])


async def test_hot_tickers_ranked_by_credibility_weighted_heat(session_factory):
    await _seed(session_factory)
    hot = await analytics.hot_tickers(session_factory, since=SINCE)
    syms = [h["symbol"] for h in hot]
    assert "NVDA" in syms and "AMD" in syms
    assert syms.index("NVDA") < syms.index("AMD")  # NVDA: 2 mentions incl HOT account
    nvda = next(h for h in hot if h["symbol"] == "NVDA")
    assert nvda["mentions"] == 2 and nvda["sentiment"] > 0 and "whale" in nvda["top_accounts"]


async def test_window_excludes_old_posts(session_factory):
    await _seed(session_factory)
    tight = NOW - timedelta(minutes=1)  # posts are 5-10 min old -> excluded
    assert await analytics.hot_tickers(session_factory, since=tight) == []
