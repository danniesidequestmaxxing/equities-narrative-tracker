"""M9 alpha ledger: event-study math, the outcomes job, and the scoreboard."""

from datetime import datetime, timedelta, timezone

from narrative_tracker import jobs
from narrative_tracker.admin.commands import handle_command
from narrative_tracker.analyze.outcomes import anchor_index, forward_returns, signed_excess
from narrative_tracker.db import idempotency, repo
from narrative_tracker.db import outcomes as db_outcomes
from narrative_tracker.db.scoreboard import _aggregate, account_scoreboard

ADMIN = [999]
D0 = datetime(2026, 6, 1, tzinfo=timezone.utc)  # bar 0 date


def _bars(closes, start=D0):
    return [
        {"ts": int((start + timedelta(days=i)).timestamp()), "o": c, "h": c, "l": c, "c": c, "v": 1000}
        for i, c in enumerate(closes)
    ]


# ----------------------------------------------------------------- pure math

def test_anchor_is_first_close_on_or_after_post_date():
    bars = _bars([100, 101, 102, 103])
    assert anchor_index(bars, D0 + timedelta(days=1, hours=14)) == 1   # intraday post -> same-day close
    assert anchor_index(bars, D0 - timedelta(days=2)) == 0             # pre-history post -> first bar
    assert anchor_index(bars, D0 + timedelta(days=9)) is None          # newer than all bars


def test_forward_returns_and_partial_completion():
    bars = _bars([100, 110, 121, 121, 121, 133.1])
    out = forward_returns(bars, D0)  # anchor = bar 0 @ 100
    assert out["px_post"] == 100
    assert abs(out["fwd"][1] - 0.10) < 1e-9
    assert abs(out["fwd"][3] - 0.21) < 1e-9
    assert abs(out["fwd"][5] - 0.331) < 1e-9
    partial = forward_returns(bars, D0 + timedelta(days=4))  # anchor = bar 4, only 1 fwd bar
    assert partial["fwd"][1] is not None and partial["fwd"][3] is None and partial["fwd"][5] is None


def test_signed_excess_directions():
    assert signed_excess("bullish", 0.05, 0.01) == 0.04    # beat the market, long
    assert signed_excess("bearish", -0.05, 0.01) == 0.06   # fell while market rose: great short
    assert signed_excess("bearish", 0.03, 0.0) == -0.03    # rose against a short call
    assert signed_excess("neutral", 0.05, 0.0) is None     # no direction, no edge
    assert signed_excess("bullish", None, 0.0) is None


# ----------------------------------------------------------------- job e2e

class FakeMarket:
    """Deterministic bars: symbol decides the path; SPY is flat-ish."""

    def __init__(self):
        self.calls = []
        self.series = {
            "SPY": [500 * (1.001 ** i) for i in range(12)],
            "WINR": [10 * (1.05 ** i) for i in range(12)],    # strong uptrend
            "LOSR": [20 * (0.97 ** i) for i in range(12)],    # downtrend
        }

    async def fetch_bars(self, symbol, *, days=150, adjusted=False):
        assert adjusted is True
        self.calls.append(symbol)
        if symbol not in self.series:
            raise RuntimeError("no data")
        return _bars(self.series[symbol])


async def _seed_mentions(sf):
    whale = await repo.get_or_create_account(sf, platform_user_id="whale", handle="whale", tier="HOT")
    mike = await repo.get_or_create_account(sf, platform_user_id="mike", handle="mike", tier="WARM")
    rows = [
        (whale, "p1", "WINR", "bullish", D0 + timedelta(days=1)),   # right call
        (whale, "p2", "LOSR", "bearish", D0 + timedelta(days=1)),   # right call
        (mike, "p3", "WINR", "bearish", D0 + timedelta(days=2)),    # wrong call
        (mike, "p4", "GONE", "bullish", D0 + timedelta(days=2)),    # no bars available
    ]
    for acct, pid, sym, stance, ts in rows:
        post_id, _ = await idempotency.insert_post_if_new(
            sf, account_id=acct, platform_post_id=pid, text=f"${sym} take", posted_at=ts)
        await repo.add_mentions(sf, post_id=post_id, mentions=[
            {"symbol": sym, "asset_class": "equity", "stance": stance,
             "stance_confidence": 0.9, "mention_confidence": 0.9}])
    # crypto mention must be excluded from the work list entirely
    p5, _ = await idempotency.insert_post_if_new(
        sf, account_id=whale, platform_post_id="p5", text="$BTC", posted_at=D0 + timedelta(days=1))
    await repo.add_mentions(sf, post_id=p5, mentions=[
        {"symbol": "BTC", "asset_class": "crypto", "stance": "bullish",
         "stance_confidence": 0.9, "mention_confidence": 0.9}])


async def test_run_outcomes_backfills_and_skips_gracefully(session_factory):
    await _seed_mentions(session_factory)
    market = FakeMarket()
    now = D0 + timedelta(days=11)
    out = await jobs.run_outcomes(session_factory, market, now=now)
    assert out["computed"] == 3 and out["pending"] == 1        # GONE has no bars
    assert "SPY" in market.calls and "BTC" not in market.calls  # bench fetched; crypto excluded

    rows = await db_outcomes.outcomes_for_accounts(session_factory, since=D0)
    by = {(r["handle"], r["symbol"]): r for r in rows}
    winr = by[("whale", "WINR")]
    assert abs(winr["fwd_3d"] - (1.05 ** 3 - 1)) < 1e-6
    assert abs(winr["bench_3d"] - (1.001 ** 3 - 1)) < 1e-6
    # bullish on an uptrend beats the flat benchmark
    assert signed_excess("bullish", winr["fwd_3d"], winr["bench_3d"]) > 0

    # second run: nothing new to compute except the still-missing symbol
    again = await jobs.run_outcomes(session_factory, market, now=now)
    assert again["computed"] == 0 and again["pending"] == 1


# ----------------------------------------------------------------- scoreboard

def _orow(handle, tier, symbol, stance, fwd3, bench3=0.0):
    return {"handle": handle, "tier": tier, "symbol": symbol, "stance": stance,
            "posted_at": D0, "fwd_1d": fwd3, "fwd_3d": fwd3, "fwd_5d": fwd3,
            "bench_1d": bench3, "bench_3d": bench3, "bench_5d": bench3}


def test_aggregate_ranks_by_signed_edge_and_quarantines_thin_samples():
    rows = [
        _orow("whale", "HOT", "A", "bullish", 0.10),
        _orow("whale", "HOT", "B", "bearish", -0.08),   # good short: +8% edge
        _orow("whale", "HOT", "C", "bullish", -0.02),   # miss
        _orow("mike", "WARM", "D", "bullish", 0.01),    # only 1 row -> thin
    ]
    board = _aggregate(rows, min_n=3)
    assert [a["handle"] for a in board["ranked"]] == ["whale"]
    whale = board["ranked"][0]
    assert whale["n"] == 3 and abs(whale["avg_3d"] - (0.10 + 0.08 - 0.02) / 3) < 1e-4
    assert abs(whale["hit_3d"] - 2 / 3) < 1e-3
    assert whale["best"]["symbol"] == "A" and whale["worst"]["symbol"] == "C"
    assert board["thin"][0]["handle"] == "mike"


async def test_scoreboard_commands_end_to_end(session_factory):
    await _seed_mentions(session_factory)
    await jobs.run_outcomes(session_factory, FakeMarket(), now=D0 + timedelta(days=11))

    board = await handle_command("/scoreboard 60", 999, session_factory, ADMIN)
    assert "Scoreboard" in board and "@whale" in board
    assert "thin sample" in board  # mike has n<3

    acct = await handle_command("/account whale 60", 999, session_factory, ADMIN)
    assert "@whale" in acct and "scored mentions: 2" in acct and "$WINR" in acct


async def test_scoreboard_empty_state(session_factory):
    msg = await handle_command("/scoreboard", 999, session_factory, ADMIN)
    assert "No scored mentions yet" in msg


async def test_run_outcomes_defers_when_benchmark_unavailable(session_factory):
    class NoSpy(FakeMarket):
        async def fetch_bars(self, symbol, *, days=150, adjusted=False):
            if symbol == "SPY":
                raise RuntimeError("429")
            return await super().fetch_bars(symbol, days=days, adjusted=adjusted)

    await _seed_mentions(session_factory)
    out = await jobs.run_outcomes(session_factory, NoSpy(), now=D0 + timedelta(days=11))
    assert out["computed"] == 0  # deferred, not degraded
    assert await db_outcomes.outcomes_for_accounts(session_factory, since=D0) == []
