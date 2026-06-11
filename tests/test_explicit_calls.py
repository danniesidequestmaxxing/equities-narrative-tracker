"""M9-C explicit-call ledger: scoring math, the scan job, and scoreboard merge."""

from datetime import datetime, timedelta, timezone

from narrative_tracker import jobs
from narrative_tracker.admin.commands import handle_command
from narrative_tracker.analyze.outcomes import stated_call_outcome
from narrative_tracker.db import calls as db_calls
from narrative_tracker.db import idempotency, repo
from narrative_tracker.extract.calls_llm import CallExtraction, StatedCall, horizon_days
from narrative_tracker.notify.telegram_bot import AlertNotifier

ADMIN = [999]
D0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _bars(rows, start=D0):
    # rows: list of (l, h, c)
    return [
        {"ts": int((start + timedelta(days=i)).timestamp()), "o": c, "h": h, "l": l, "c": c, "v": 1}
        for i, (l, h, c) in enumerate(rows)
    ]


# ------------------------------------------------------------- scoring math

def test_target_hit_with_r():
    bars = _bars([(99, 101, 100), (99, 102, 101), (100, 106, 105)])
    out = stated_call_outcome(bars, stated_at=D0, direction="long",
                              entry=100.0, stop=98.0, targets=[105.0], horizon_days=10)
    assert out["reason"] == "target"
    assert abs(out["realized_r"] - 2.5) < 1e-9       # (105-100)/(100-98)
    assert abs(out["realized_pct"] - 0.05) < 1e-9


def test_stop_hit_and_conservative_tiebreak():
    # day 2 touches both stop (97) and target (106): stop wins
    bars = _bars([(99, 101, 100), (99, 101, 100), (96, 107, 100)])
    out = stated_call_outcome(bars, stated_at=D0, direction="long",
                              entry=100.0, stop=97.0, targets=[106.0], horizon_days=10)
    assert out["reason"] == "stop" and out["realized_r"] == -1.0


def test_short_direction_and_timeout():
    bars = _bars([(99, 101, 100)] * 4 + [(89, 91, 90)] * 2)
    out = stated_call_outcome(bars, stated_at=D0, direction="short",
                              entry=100.0, stop=None, targets=[], horizon_days=5)
    assert out["reason"] == "timeout"
    assert out["realized_r"] is None                  # no stop stated -> no R
    assert abs(out["realized_pct"] - 0.10) < 1e-9     # shorted 100 -> 90 = +10%


def test_still_open_returns_none():
    bars = _bars([(99, 101, 100), (99, 101, 100)])
    assert stated_call_outcome(bars, stated_at=D0, direction="long",
                               entry=None, stop=None, targets=[], horizon_days=10) is None


def test_horizon_parser():
    assert horizon_days("0dte lotto") == 2
    assert horizon_days("swing into Friday") == 10
    assert horizon_days("earnings play") == 21
    assert horizon_days("2027 leaps") == 60
    assert horizon_days(None) == 10


# ------------------------------------------------------------- scan job e2e

class FakeExtractor:
    def __init__(self):
        self.seen = []

    async def __call__(self, text):
        self.seen.append(text)
        if "ERROR" in text:
            raise RuntimeError("llm down")
        if "long $WINR" in text:
            return CallExtraction(has_call=True, calls=[
                StatedCall(symbol="$winr", direction="long", entry=10.0, stop=9.0,
                           targets=[12.0], horizon="swing", confidence=0.9),
                StatedCall(symbol="MAYBE", direction="long", confidence=0.3),  # below threshold
            ])
        return CallExtraction(has_call=False)


async def _seed_posts(sf):
    whale = await repo.get_or_create_account(sf, platform_user_id="whale", handle="whale", tier="HOT")
    texts = {
        "c1": "long $WINR at 10, stop 9, target 12. swing.",
        "c2": "nothing to see, $SPY chop",
        "c3": "ERROR poison post $X",
    }
    ids = {}
    for pid, text in texts.items():
        post_id, _ = await idempotency.insert_post_if_new(
            sf, account_id=whale, platform_post_id=pid, text=text, posted_at=D0 + timedelta(days=1))
        await repo.add_mentions(sf, post_id=post_id, mentions=[
            {"symbol": "WINR", "asset_class": "equity", "stance": "bullish",
             "stance_confidence": 0.9, "mention_confidence": 0.9}])
        ids[pid] = post_id
    return whale, ids


async def test_call_scan_extracts_notifies_and_retries_errors(session_factory, fake_bot):
    await _seed_posts(session_factory)
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    ex = FakeExtractor()

    out = await jobs.run_call_scan(session_factory, ex, notifier)
    assert out["scanned"] == 2 and out["calls"] == 1       # poison post left unscanned
    open_now = await db_calls.open_calls(session_factory)
    assert len(open_now) == 1 and open_now[0]["symbol"] == "WINR"  # low-conf MAYBE filtered
    assert any("STATED CALL" in m["text"] or "Stated call" in m["text"] for m in fake_bot.sent)

    # next cycle: only the poison post is retried; no duplicate calls or messages
    out2 = await jobs.run_call_scan(session_factory, ex, notifier)
    assert out2["scanned"] == 0 and out2["calls"] == 0
    assert len(await db_calls.open_calls(session_factory)) == 1
    assert len(fake_bot.sent) == 1


class CallMarket:
    """WINR runs 10 -> 12+ so the stated target (12) gets hit."""

    async def fetch_bars(self, symbol, *, days=150, adjusted=False):
        series = {
            "SPY": [(499, 501, 500)] * 12,
            "WINR": [(10.0 + 0.3 * i - 0.2, 10.0 + 0.3 * i + 0.2, 10.0 + 0.3 * i) for i in range(12)],
        }
        if symbol not in series:
            raise RuntimeError("no data")
        return _bars(series[symbol])


async def test_outcomes_job_closes_stated_calls_and_scoreboard_shows_them(session_factory, fake_bot):
    _, ids = await _seed_posts(session_factory)
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    await jobs.run_call_scan(session_factory, FakeExtractor(), notifier)

    out = await jobs.run_outcomes(session_factory, CallMarket(), now=D0 + timedelta(days=11))
    assert out["stated_closed"] == 1
    assert await db_calls.open_calls(session_factory) == []

    stats = await db_calls.stated_stats(session_factory, since=D0)
    assert stats["whale"]["closed"] == 1 and stats["whale"]["hit"] == 1.0
    assert abs(stats["whale"]["avg_r"] - 2.0) < 1e-6       # (12-10)/(10-9)

    board = await handle_command("/scoreboard 60", 999, session_factory, ADMIN)
    assert "stated calls" in board and "@whale" in board and "+2.0R" in board
    acct = await handle_command("/account whale 60", 999, session_factory, ADMIN)
    assert "stated calls" in acct
