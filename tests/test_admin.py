"""Live admin commands (/addsource etc.) + the dynamic watchlist."""

from narrative_tracker.admin.commands import handle_command
from narrative_tracker.db import repo

ADMIN = [999]


async def test_addsource_adds_to_dynamic_watchlist(session_factory):
    r = await handle_command("/addsource @whale tier=HOT", 999, session_factory, ADMIN)
    assert "Watching @whale" in r and "HOT" in r
    assert "whale" in await repo.active_handles(session_factory)  # poller will see it
    listed = await handle_command("/sources", 999, session_factory, ADMIN)
    assert "@whale" in listed and "HOT" in listed


async def test_non_admin_is_rejected(session_factory):
    r = await handle_command("/addsource @x", 123, session_factory, ADMIN)
    assert "Not authorized" in r
    assert await repo.active_handles(session_factory) == []  # nothing happened


async def test_no_admins_configured_locks_everything(session_factory):
    assert "Not authorized" in await handle_command("/addsource @x", 999, session_factory, [])


async def test_rmsource_removes_from_watchlist(session_factory):
    await handle_command("/addsource @whale", 999, session_factory, ADMIN)
    r = await handle_command("/rmsource @whale", 999, session_factory, ADMIN)
    assert "Stopped watching" in r
    assert await repo.active_handles(session_factory) == []


async def test_tier_and_status(session_factory):
    await handle_command("/addsource @whale tier=COLD", 999, session_factory, ADMIN)
    assert "WARM" in await handle_command("/tier @whale WARM", 999, session_factory, ADMIN)
    assert "watching 1" in await handle_command("/status", 999, session_factory, ADMIN)


# --- seamless input: @handle / $TICKER, ticker watchlist ----------------------


async def test_plain_at_handle_adds_account(session_factory):
    r = await handle_command("@whale hot", 999, session_factory, ADMIN)
    assert "Watching @whale" in r and "HOT" in r
    assert "whale" in await repo.active_handles(session_factory)


async def test_plain_at_handle_defaults_cold(session_factory):
    assert "COLD" in await handle_command("@mike", 999, session_factory, ADMIN)


async def test_watch_unwatch_watching_flow(session_factory):
    r = await handle_command("$nvda watch", 999, session_factory, ADMIN)
    assert "Watching $NVDA" in r
    assert await repo.watched_tickers(session_factory) == ["NVDA"]
    assert await repo.is_watched(session_factory, "$nvda") is True

    listed = await handle_command("/watching", 999, session_factory, ADMIN)
    assert "$NVDA" in listed

    r = await handle_command("$NVDA unwatch", 999, session_factory, ADMIN)
    assert "Stopped watching" in r
    assert await repo.watched_tickers(session_factory) == []
    assert "empty" in await handle_command("/watching", 999, session_factory, ADMIN)


async def test_slash_watch_equivalent(session_factory):
    await handle_command("/watch AMD", 999, session_factory, ADMIN)
    assert await repo.watched_tickers(session_factory) == ["AMD"]


class _FakeMarket:
    async def fetch_bars(self, symbol, *, days=400, adjusted=False):
        px, out = 100.0, []
        for i in range(300):
            px += 0.5
            out.append({"ts": i, "o": px, "h": px + 1, "l": px - 1, "c": px, "v": 1000.0})
        return out

    async def fetch_overview(self, symbol):
        return {"name": symbol, "market_cap": 4.6e12, "sector": "Semiconductors", "exchange": "XNAS"}


async def test_dollar_ticker_returns_brief(session_factory):
    from datetime import datetime, timedelta, timezone

    from narrative_tracker.db import idempotency

    acct = await repo.get_or_create_account(session_factory, platform_user_id="whale", handle="whale", tier="HOT")
    pid, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=acct, platform_post_id="1", text="$NVDA breaking out",
        posted_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await repo.add_mentions(session_factory, post_id=pid, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.9, "mention_confidence": 0.95}])

    brief = await handle_command("$NVDA", 999, session_factory, ADMIN, market=_FakeMarket())
    assert "$NVDA" in brief and "sentiment" in brief
    assert "@whale" in brief and "breaking out" in brief    # the takes
    assert "RSI" in brief and "Semiconductors" in brief     # TA + fundamentals
    assert "tradingview.com" in brief


async def test_dollar_ticker_brief_without_market(session_factory):
    brief = await handle_command("$TSLA", 999, session_factory, ADMIN)
    assert "$TSLA" in brief and "no mentions" in brief and "RSI" not in brief


async def test_plain_input_still_requires_admin(session_factory):
    assert "Not authorized" in await handle_command("@whale", 123, session_factory, ADMIN)
    assert "Not authorized" in await handle_command("$NVDA watch", 123, session_factory, ADMIN)
    assert await repo.watched_tickers(session_factory) == []
