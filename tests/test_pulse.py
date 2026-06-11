"""8h Pulse: early radar, TA snapshot, formatting, and the end-to-end job."""

from datetime import datetime, timedelta, timezone

from narrative_tracker import jobs
from narrative_tracker.analyze import pulse
from narrative_tracker.analyze.technicals import snapshot_from_bars
from narrative_tracker.db import idempotency, repo
from narrative_tracker.notify.telegram_bot import AlertNotifier

NOW = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)


def _row(symbol, handle, *, hours_ago, stance="bullish", post_id=None, tier="COLD", text=None):
    return {
        "symbol": symbol, "asset_class": "equity", "stance": stance,
        "stance_confidence": 0.8, "mention_confidence": 0.9,
        "text": text or f"${symbol} take", "posted_at": NOW - timedelta(hours=hours_ago),
        "platform_post_id": post_id or f"{handle}-{symbol}-{hours_ago}",
        "handle": handle, "tier": tier,
    }


# ---------------------------------------------------------------- early radar

def test_early_radar_flags_first_appearance_and_acceleration():
    window = [
        _row("SIVE", "a", hours_ago=1), _row("SIVE", "b", hours_ago=2),   # new, 2 accounts
        _row("NVDA", "a", hours_ago=3), _row("NVDA", "b", hours_ago=1),
        _row("NVDA", "c", hours_ago=2), _row("NVDA", "a", hours_ago=4, post_id="x"),
        _row("AAPL", "a", hours_ago=5),                                   # steady old name
    ]
    prior = [
        _row("NVDA", "a", hours_ago=30),  # 1 mention all week -> 4 in 8h is a burst
        *[_row("AAPL", "a", hours_ago=20 + i * 10, post_id=f"ap{i}") for i in range(6)],
    ]
    out = pulse.early_radar(window, prior, window_hours=8)
    by_sym = {e["symbol"]: e for e in out}
    assert by_sym["SIVE"]["kind"] == "new" and by_sym["SIVE"]["accounts"] == 2
    assert by_sym["NVDA"]["kind"] == "accelerating"  # 4 in 8h vs 1 in the prior week
    assert "AAPL" not in by_sym  # steady rate, not accelerating


def test_early_radar_multi_account_ranks_first():
    window = [
        _row("ONE", "solo", hours_ago=1), _row("ONE", "solo", hours_ago=2, post_id="s2"),
        _row("TWO", "a", hours_ago=1), _row("TWO", "b", hours_ago=2),
    ]
    out = pulse.early_radar(window, [], window_hours=8)
    assert out[0]["symbol"] == "TWO"  # 2 accounts beats 1 account x 2 posts


# ----------------------------------------------------------------- TA snapshot

def _bars(n=300, start=100.0, step=0.5):
    out = []
    px = start
    for i in range(n):
        px += step
        out.append({"ts": i, "o": px - 0.2, "h": px + 1.0, "l": px - 1.0, "c": px, "v": 1000.0 + (500 if i == n - 1 else 0)})
    return out


def test_snapshot_from_bars_uptrend():
    snap = snapshot_from_bars(_bars())
    assert snap["trend"] == "up" and snap["rsi"] and snap["rsi"] > 60
    assert snap["chg_5d"] > 0 and snap["vol_ratio"] > 1.0
    assert snap["off_high_pct"] is not None and snap["support"] < snap["price"] <= snap["resistance"]


def test_snapshot_from_bars_too_short():
    assert snapshot_from_bars(_bars(n=10)) is None


# ----------------------------------------------------------------- formatting

def test_build_pulse_renders_all_sections_and_fits_telegram():
    brief = pulse.PulseBrief(
        headline="AI capex chatter re-accelerating",
        narratives=[pulse.NarrativeNote(title="AI infrastructure", takeaway="Watch NVDA over 195.", tickers=["NVDA", "MU"])],
        early_radar="SIVE first appearance across two accounts.",
    )
    mdv2, plain = pulse.build_pulse(
        window_label="8h", date_label="2026-06-11 16:00 UTC",
        posts_count=12, accounts_count=3, tickers_count=5,
        hot=[{"symbol": "NVDA", "asset_class": "equity", "mentions": 6, "accounts": 3,
              "sentiment": 0.42, "n_eff": 3.1, "heat": 2.5, "top_accounts": ["a"], "delta": "up"}],
        brief=brief, narratives=[], early=[],
        deep_dives=[{"symbol": "NVDA", "ta": snapshot_from_bars(_bars()), "name": "NVIDIA",
                     "market_cap": 4.6e12, "sector": "Semiconductors", "exchange": "XNAS"}],
        recap=[{"handle": "whale", "posts": 7, "symbols": [{"symbol": "NVDA", "emoji": "🟢"}]}],
        quiet=["sleepy"],
    )
    for must in ("8h Pulse", "AI capex", "AI infrastructure", "NVDA", "Early radar",
                 "SIVE first appearance", "Semiconductors", "4\\.6T", "whale", "quiet"):
        assert must in mdv2, must
    assert "not financial advice" in plain
    assert len(mdv2) < 4096


def test_build_pulse_fallback_and_hints_without_llm_or_market():
    mdv2, _ = pulse.build_pulse(
        window_label="8h", date_label="2026-06-11 16:00 UTC",
        posts_count=2, accounts_count=1, tickers_count=1,
        hot=[], brief=None,
        narratives=[{"title": "AI infrastructure", "tickers": ["NVDA"], "takeaway": "3 mentions, net bullish lean."}],
        early=[{"symbol": "SIVE", "kind": "new", "mentions": 2, "accounts": 2, "handles": ["a", "b"]}],
        deep_dives=[], recap=[], quiet=[], market_hint=True, llm_hint=True,
    )
    assert "AI infrastructure" in mdv2
    assert "NT\\_LLM\\_MODEL" in mdv2 and "NT\\_POLYGON\\_API\\_KEY" in mdv2
    assert "first appearance" in mdv2


def test_cap_lines_trims_to_telegram_limit():
    lines = ["header", "stats"] + [f"line {i} " + "x" * 80 for i in range(120)] + ["", "_footer_"]
    out = pulse._cap_lines(list(lines))
    assert len(out) <= 3900 and out.endswith("_footer_") and out.startswith("header")


# ------------------------------------------------------------------ end-to-end

class FakeMarket:
    async def fetch_bars(self, symbol, *, days=400, adjusted=False):
        assert adjusted is True  # pulse must use split-adjusted bars
        return _bars()

    async def fetch_overview(self, symbol):
        return {"name": symbol, "market_cap": 1.2e9, "sector": "Semiconductors", "exchange": "XNAS"}


async def _seed(sf):
    whale = await repo.get_or_create_account(sf, platform_user_id="whale", handle="whale", tier="HOT")
    mike = await repo.get_or_create_account(sf, platform_user_id="mike", handle="mike", tier="WARM")
    quiet = await repo.get_or_create_account(sf, platform_user_id="sleepy", handle="sleepy", tier="COLD")
    p1, _ = await idempotency.insert_post_if_new(
        sf, account_id=whale, platform_post_id="1", text="$NVDA AI capex is back", posted_at=NOW - timedelta(hours=2))
    await repo.add_mentions(sf, post_id=p1, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.9, "mention_confidence": 0.95}])
    p2, _ = await idempotency.insert_post_if_new(
        sf, account_id=mike, platform_post_id="2", text="$SIVE new position", posted_at=NOW - timedelta(hours=1))
    await repo.add_mentions(sf, post_id=p2, mentions=[
        {"symbol": "SIVE", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.8, "mention_confidence": 0.9}])
    # old NVDA post (prior week baseline; also makes NVDA "not new")
    p3, _ = await idempotency.insert_post_if_new(
        sf, account_id=whale, platform_post_id="3", text="$NVDA earlier take", posted_at=NOW - timedelta(days=3))
    await repo.add_mentions(sf, post_id=p3, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish", "stance_confidence": 0.7, "mention_confidence": 0.9}])


async def test_run_pulse_end_to_end(session_factory, fake_bot):
    await _seed(session_factory)
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)

    async def writer(context):
        assert "@whale" in context and "$NVDA" in context  # real window data reaches the LLM
        return pulse.PulseBrief(
            headline="AI capex back on the tape",
            narratives=[pulse.NarrativeNote(title="AI infrastructure", takeaway="Watch follow-through.", tickers=["NVDA"])],
        )

    async def watchlist():
        return ["whale", "mike", "sleepy"]

    out = await jobs.run_pulse(
        session_factory, notifier, now=NOW, market=FakeMarket(), writer=writer,
        watchlist_provider=watchlist,
    )
    assert out["broadcast"] is True and out["llm"] is True
    assert out["posts"] == 2 and out["deep_dives"] >= 1 and "SIVE" in out["early"]
    text = fake_bot.sent[0]["text"]
    assert "Pulse" in text and "NVDA" in text and "sleepy" in text  # quiet account named

    # same window again -> idempotent no-op
    again = await jobs.run_pulse(
        session_factory, notifier, now=NOW, market=FakeMarket(), writer=writer,
        watchlist_provider=watchlist,
    )
    assert again["broadcast"] is False and len(fake_bot.sent) == 1


async def test_run_pulse_quiet_window_sends_nothing(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    out = await jobs.run_pulse(session_factory, notifier, now=NOW)
    assert out == {"skipped": "no_posts"} and fake_bot.sent == []


async def test_run_pulse_degrades_without_market_or_llm(session_factory, fake_bot):
    await _seed(session_factory)
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)

    async def broken_writer(context):
        raise RuntimeError("provider down")

    out = await jobs.run_pulse(session_factory, notifier, now=NOW, writer=broken_writer)
    assert out["broadcast"] is True and out["llm"] is False and out["deep_dives"] == 0
    assert "Pulse" in fake_bot.sent[0]["text"]  # still delivered, fallback narrative


async def test_run_pulse_always_deep_dives_watched_tickers(session_factory, fake_bot):
    await _seed(session_factory)
    await repo.watch_ticker(session_factory, "$AMD")  # watched but NOT mentioned this window
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)

    out = await jobs.run_pulse(session_factory, notifier, now=NOW, market=FakeMarket())
    assert out["broadcast"] is True
    text = fake_bot.sent[0]["text"]
    assert "AMD" in text and "🔔" in text  # pinned into the deep-dive despite zero mentions
