"""M10: engagement capture, reversal alerts, evidence credibility, account splits."""

from datetime import datetime, timedelta, timezone

from narrative_tracker import jobs
from narrative_tracker.admin.commands import handle_command
from narrative_tracker.db import calls as db_calls
from narrative_tracker.db import analytics, idempotency, repo
from narrative_tracker.db.scoreboard import _detail_splits
from narrative_tracker.ingest.polling_client import _metrics, _to_rawpost
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.score.credibility import evidence_credibility
from narrative_tracker.worker import process_post

ADMIN = [999]
T0 = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _post(text, *, post_id, posted_at, metrics=None, handle="whale"):
    return RawPost(
        platform_user_id=handle, handle=handle, platform_post_id=post_id,
        text=text, posted_at=posted_at, metrics=metrics or {},
    )


# ------------------------------------------------------- engagement capture

def test_metrics_extraction_handles_field_variants():
    assert _metrics({"likeCount": 42, "retweetCount": 7, "viewCount": 9000}) == {
        "likes": 42, "retweets": 7, "views": 9000}
    assert _metrics({"favorite_count": 3, "reply_count": 1}) == {"likes": 3, "replies": 1}
    assert _metrics({"likeCount": "not-a-number"}) == {}
    raw = _to_rawpost({"id": "1", "text": "$NVDA", "createdAt": "2026-06-10 12:00:00",
                       "author": {"userName": "whale"}, "likeCount": 5, "viewCount": 100})
    assert raw.metrics == {"likes": 5, "views": 100}


async def test_engagement_saved_once(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    post = _post("$NVDA breaking out", post_id="e1", posted_at=T0, metrics={"likes": 12, "views": 800})
    await process_post(post, session_factory=session_factory, notifier=notifier)
    await process_post(post, session_factory=session_factory, notifier=notifier)  # re-sighting

    from narrative_tracker.db.models import PostEngagement
    from sqlalchemy import select
    async with session_factory() as session:
        rows = (await session.scalars(select(PostEngagement))).all()
    assert len(rows) == 1 and rows[0].likes == 12 and rows[0].views == 800


# ---------------------------------------------------------- reversal alerts

async def test_reversal_fires_on_stance_flip(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    await process_post(_post("long $NVDA here, ripping", post_id="r1", posted_at=T0),
                       session_factory=session_factory, notifier=notifier)
    assert not [m for m in fake_bot.sent if "REVERSAL" in m["text"] or "Reversal" in m["text"]]

    await process_post(_post("$NVDA looks dead, selling everything. avoid", post_id="r2",
                             posted_at=T0 + timedelta(hours=5)),
                       session_factory=session_factory, notifier=notifier)
    flips = [m for m in fake_bot.sent if "Reversal" in m["text"] or "REVERSAL" in m["text"]]
    assert len(flips) == 1 and "NVDA" in flips[0]["text"] and "bullish" in flips[0]["text"]

    # reprocessing the same post never duplicates the reversal
    await process_post(_post("$NVDA looks dead, selling everything. avoid", post_id="r2",
                             posted_at=T0 + timedelta(hours=5)),
                       session_factory=session_factory, notifier=notifier)
    assert len([m for m in fake_bot.sent if "Reversal" in m["text"] or "REVERSAL" in m["text"]]) == 1


async def test_reversal_fires_against_open_stated_call(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    # the stated call exists, but no prior mention in the window
    acct = await repo.get_or_create_account(session_factory, platform_user_id="whale", handle="whale", tier="HOT")
    seed_post, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=acct, platform_post_id="c0", text="long $WINR at 10",
        posted_at=T0 - timedelta(days=30))
    await db_calls.save_call(
        session_factory, post_id=seed_post, account_id=acct, symbol="WINR", direction="long",
        entry=10.0, stop=9.0, targets=[12.0], horizon_raw="swing", horizon_days=10,
        is_option=False, confidence=0.9, stated_at=T0 - timedelta(days=30))

    await process_post(_post("$WINR is done, selling. avoid this dump", post_id="c1", posted_at=T0),
                       session_factory=session_factory, notifier=notifier)
    flips = [m for m in fake_bot.sent if "Reversal" in m["text"] or "REVERSAL" in m["text"]]
    assert len(flips) == 1
    assert "STATED CALL" in flips[0]["text"].upper() or "stated call" in flips[0]["text"]


# ----------------------------------------------------- evidence credibility

def test_evidence_credibility_blends_and_clamps():
    assert evidence_credibility("HOT") == 0.6                      # no evidence -> prior
    up = evidence_credibility("COLD", event_n=8, event_edge=0.03)  # +3% avg edge on n=8
    assert up > 0.15
    down = evidence_credibility("HOT", event_n=8, event_edge=-0.03)
    assert down < 0.6
    stated = evidence_credibility("COLD", stated_n=8, stated_avg_r=1.5)
    event = evidence_credibility("COLD", event_n=8, event_edge=0.02)
    assert stated > event                                          # stated calls weigh double
    assert evidence_credibility("COLD", event_n=1000, event_edge=-0.5) >= 0.05
    assert evidence_credibility("HOT", stated_n=1000, stated_avg_r=10.0) <= 0.95


async def test_run_outcomes_refreshes_live_credibility(session_factory):
    from tests.test_outcomes import FakeMarket, _seed_mentions, D0

    await _seed_mentions(session_factory)
    out = await jobs.run_outcomes(session_factory, FakeMarket(), now=D0 + timedelta(days=11))
    assert out["credibility_updated"] == 2  # whale + mike have evidence

    whale_id = await repo.get_account_id(session_factory, platform_user_id="whale")
    mike_id = await repo.get_account_id(session_factory, platform_user_id="mike")
    whale_cred = await repo.get_credibility(session_factory, account_id=whale_id, as_of=D0 + timedelta(days=12))
    mike_cred = await repo.get_credibility(session_factory, account_id=mike_id, as_of=D0 + timedelta(days=12))
    assert whale_cred > 0.6   # two right calls lift a HOT prior
    assert mike_cred < 0.35   # one wrong call sinks a WARM prior

    # analytics reads the live scores now
    detail = await analytics.ticker_detail(session_factory, symbol="WINR", since=D0)
    whale_take = next(t for t in detail["takes"] if t["handle"] == "whale")
    assert abs(whale_take["credibility"] - round(whale_cred, 2)) < 1e-9


# ------------------------------------------------------------ account splits

def test_detail_splits_by_symbol_and_first_vs_repeat():
    def row(sym, ts_days, fwd):
        return {"symbol": sym, "stance": "bullish", "posted_at": T0 + timedelta(days=ts_days),
                "fwd_3d": fwd, "bench_3d": 0.0}
    rows = [row("A", 0, 0.10), row("A", 1, 0.02), row("A", 2, 0.00), row("B", 3, 0.04)]
    s = _detail_splits(rows)
    assert s["by_symbol"][0]["symbol"] == "A" and s["by_symbol"][0]["n"] == 3
    assert s["first"]["n"] == 2 and abs(s["first"]["avg"] - 0.07) < 1e-9    # A@0.10, B@0.04
    assert s["repeat"]["n"] == 2 and abs(s["repeat"]["avg"] - 0.01) < 1e-9  # A repeats


async def test_account_command_shows_splits_and_decay(session_factory):
    from tests.test_outcomes import FakeMarket, _seed_mentions, D0

    await _seed_mentions(session_factory)
    # a repeat mention so the first-vs-repeat split has both sides
    whale_id = await repo.get_account_id(session_factory, platform_user_id="whale")
    p6, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=whale_id, platform_post_id="p6",
        text="$WINR still going", posted_at=D0 + timedelta(days=3))
    await repo.add_mentions(session_factory, post_id=p6, mentions=[
        {"symbol": "WINR", "asset_class": "equity", "stance": "bullish",
         "stance_confidence": 0.9, "mention_confidence": 0.9}])

    await jobs.run_outcomes(session_factory, FakeMarket(), now=D0 + timedelta(days=11))
    acct = await handle_command("/account whale 60", 999, session_factory, ADMIN)
    assert "by symbol" in acct and "first mentions" in acct and "edge" in acct
