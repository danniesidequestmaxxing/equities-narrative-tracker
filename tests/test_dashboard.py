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


# --- open (public) watchlist management: anyone can add/remove; only input is validated ---

import pytest

fastapi = pytest.importorskip("fastapi")  # prod-only dep; skip where it's absent


def test_handle_regex_accepts_real_handles_and_rejects_junk():
    from narrative_tracker.api import dashboard
    assert dashboard._HANDLE_RE.match("elonmusk")
    assert dashboard._HANDLE_RE.match("a_b_123")
    assert not dashboard._HANDLE_RE.match("has space")
    assert not dashboard._HANDLE_RE.match("bad!char")
    assert not dashboard._HANDLE_RE.match("waytoolong_handle")  # >15 chars
    assert not dashboard._HANDLE_RE.match("")


async def test_add_source_rejects_invalid_handle():
    from narrative_tracker.api import dashboard
    with pytest.raises(fastapi.HTTPException) as ei:  # raises before touching the DB
        await dashboard.api_add_source(dashboard.SourceIn(handle="not a handle!", tier="HOT"))
    assert ei.value.status_code == 400


async def test_public_add_list_remove_flow(session_factory, monkeypatch):
    from narrative_tracker.api import dashboard
    monkeypatch.setattr(dashboard, "_sf", session_factory)
    added = await dashboard.api_add_source(dashboard.SourceIn(handle="@NewWhale", tier="hot"))
    assert added == {"ok": True, "handle": "newwhale", "tier": "HOT"}  # normalized, no token needed
    listing = await dashboard.api_sources()
    assert any(s["handle"] == "newwhale" and s["tier"] == "HOT" for s in listing["sources"])
    removed = await dashboard.api_remove_source("NewWhale")
    assert removed == {"ok": True}
    assert not any(s["handle"] == "newwhale" for s in (await dashboard.api_sources())["sources"])
