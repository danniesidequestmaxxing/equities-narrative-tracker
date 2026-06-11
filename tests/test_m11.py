"""M11: vision extraction, credibility-stamped alerts, divergence, weekly report."""

from datetime import datetime, timedelta, timezone

from narrative_tracker import jobs
from narrative_tracker.db import analytics, calls as db_calls, idempotency, repo
from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.extract.vision import FakeVisionExtractor, VisionItem, VisionRead, _to_mentions
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.notify.telegram_bot import AlertNotifier, build_alert
from narrative_tracker.schemas.mention import AssetClass, Mention, ResolutionMethod, Stance

T0 = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- vision

def test_vision_read_maps_to_mentions_with_stance():
    read = VisionRead(has_tickers=True, items=[
        VisionItem(symbol="$nvda", direction="long", levels=[195.0], kind="chart", confidence=0.85),
        VisionItem(symbol="SPY", direction="short", kind="position", confidence=0.7),
        VisionItem(symbol="BLUR", direction="long", confidence=0.3),       # below threshold
        VisionItem(symbol="TOOLONGSYM", direction="long", confidence=0.9),  # not a ticker
    ])
    out = _to_mentions(read, source_post_id="p1")
    assert [m.symbol for m in out] == ["NVDA", "SPY"]
    assert out[0].stance is Stance.BULLISH and out[1].stance is Stance.BEARISH
    assert out[0].resolution_method is ResolutionMethod.VISION_OCR
    assert out[0].mention_confidence <= 0.9
    assert _to_mentions(VisionRead(has_tickers=False), source_post_id="p") == []


async def test_pipeline_uses_vision_only_for_image_only_posts():
    url = "https://pbs.twimg.com/chart.png"
    canned = Mention(
        symbol="AAOI", asset_class=AssetClass.EQUITY,
        resolution_method=ResolutionMethod.VISION_OCR, mention_confidence=0.8,
        stance=Stance.BULLISH, stance_confidence=0.7, source_post_id="",
    )
    vision = FakeVisionExtractor({url: [canned]})
    pipe = ExtractionPipeline(vision=vision)

    # image-only post (no tickers in text) -> vision fires, stance survives
    mentions = await pipe.extract(text="this setup 👇", media_urls=[url])
    assert [m.symbol for m in mentions] == ["AAOI"]
    assert mentions[0].stance is Stance.BULLISH  # S3 must not overwrite vision stance
    assert vision.calls == 1

    # text already resolved a ticker -> vision is not consulted
    await pipe.extract(text="$NVDA breaking out", media_urls=[url])
    assert vision.calls == 1


# ------------------------------------------------------------ stamped alerts

def _mention(symbol="NVDA"):
    return Mention(symbol=symbol, asset_class=AssetClass.EQUITY,
                   resolution_method=ResolutionMethod.CASHTAG_EXACT,
                   mention_confidence=0.95, stance=Stance.BULLISH, stance_confidence=0.8)


def test_build_alert_stamps_author_credibility():
    post = RawPost(platform_user_id="whale", handle="whale", platform_post_id="1",
                   text="$NVDA go", posted_at=T0)
    mdv2, plain = build_alert(post, _mention(), author_stat={"score": 0.71, "n": 14})
    assert "cred 0\\.71" in mdv2 and "evidence n\\=14" in mdv2  # md() escapes '=' too
    assert "cred 0.71" in plain and "evidence n=14" in plain
    mdv2_no, plain_no = build_alert(post, _mention())
    assert "cred" not in mdv2_no and "cred" not in plain_no


async def test_send_alert_fetches_live_stat(session_factory, fake_bot):
    acct = await repo.get_or_create_account(session_factory, platform_user_id="whale", handle="whale", tier="HOT")
    await repo.insert_account_score(session_factory, account_id=acct, as_of=T0, decayed_score=0.71, sample_size=14)
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    post = RawPost(platform_user_id="whale", handle="whale", platform_post_id="s1",
                   text="$NVDA breaking out", posted_at=T0 + timedelta(hours=1))
    assert await notifier.send_alert(post, _mention())
    assert "cred 0" in fake_bot.sent[0]["text"] and "n\\=14" in fake_bot.sent[0]["text"]


# ---------------------------------------------------------------- divergence

async def _seed_divergent(sf):
    smart = await repo.get_or_create_account(sf, platform_user_id="smart", handle="smart", tier="COLD")
    await repo.insert_account_score(sf, account_id=smart, as_of=T0, decayed_score=0.8, sample_size=12)
    crowd1 = await repo.get_or_create_account(sf, platform_user_id="c1", handle="c1", tier="COLD")
    crowd2 = await repo.get_or_create_account(sf, platform_user_id="c2", handle="c2", tier="COLD")
    rows = [(smart, "d1", "bullish"), (crowd1, "d2", "bearish"), (crowd2, "d3", "bearish")]
    for acct, pid, stance in rows:
        post_id, _ = await idempotency.insert_post_if_new(
            sf, account_id=acct, platform_post_id=pid, text="$DIVR take", posted_at=T0 + timedelta(hours=1))
        await repo.add_mentions(sf, post_id=post_id, mentions=[
            {"symbol": "DIVR", "asset_class": "equity", "stance": stance,
             "stance_confidence": 0.9, "mention_confidence": 0.9}])


async def test_divergence_flags_smart_vs_crowd(session_factory):
    await _seed_divergent(session_factory)
    out = await analytics.divergence(session_factory, since=T0)
    assert len(out) == 1 and out[0]["symbol"] == "DIVR"
    assert out[0]["smart"] > 0 > out[0]["crowd"]
    assert out[0]["gap"] > 0.5 and out[0]["smart_accounts"] == ["smart"]


async def test_divergence_empty_without_smart_side(session_factory):
    # nobody above the smart threshold -> no divergence rows, ever
    c1 = await repo.get_or_create_account(session_factory, platform_user_id="c1", handle="c1", tier="COLD")
    p, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=c1, platform_post_id="x", text="$LONE", posted_at=T0)
    await repo.add_mentions(session_factory, post_id=p, mentions=[
        {"symbol": "LONE", "asset_class": "equity", "stance": "bullish",
         "stance_confidence": 0.9, "mention_confidence": 0.9}])
    assert await analytics.divergence(session_factory, since=T0 - timedelta(days=1)) == []


# -------------------------------------------------------------- weekly report

async def test_weekly_report_sends_once_per_week(session_factory, fake_bot):
    from tests.test_outcomes import FakeMarket, _seed_mentions, D0

    await _seed_mentions(session_factory)
    await jobs.run_outcomes(session_factory, FakeMarket(), now=D0 + timedelta(days=11))
    # one graded stated call for the 🎯 section
    whale_id = await repo.get_account_id(session_factory, platform_user_id="whale")
    p, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=whale_id, platform_post_id="w1", text="long $WINR",
        posted_at=D0 + timedelta(days=2))
    await db_calls.save_call(
        session_factory, post_id=p, account_id=whale_id, symbol="WINR", direction="long",
        entry=10.0, stop=9.0, targets=[12.0], horizon_raw=None, horizon_days=10,
        is_option=False, confidence=0.9, stated_at=D0 + timedelta(days=2))
    call_id = (await db_calls.open_calls(session_factory))[0]["id"]
    await db_calls.close_call(session_factory, call_id=call_id, reason="target",
                              realized_r=2.0, realized_pct=0.2, closed_at=D0 + timedelta(days=9))

    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    now = D0 + timedelta(days=12)
    out = await jobs.run_weekly_report(session_factory, notifier, now=now)
    assert out["broadcast"] is True and out["graded"] == 1
    text = fake_bot.sent[0]["text"]
    assert "Weekly alpha report" in text and "@whale" in text and "WINR" in text

    again = await jobs.run_weekly_report(session_factory, notifier, now=now + timedelta(hours=2))
    assert again["broadcast"] is False and len(fake_bot.sent) == 1  # same ISO week -> deduped


async def test_weekly_report_silent_with_no_data(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    out = await jobs.run_weekly_report(session_factory, notifier, now=T0)
    assert out == {"skipped": "no_data"} and fake_bot.sent == []


def test_weekly_report_due_window():
    from narrative_tracker.jobs import weekly_report_due

    # 2026-06-12 is a Friday; 21:00 UTC == 05:00 Saturday in Malaysia (UTC+8)
    fri = datetime(2026, 6, 12, tzinfo=timezone.utc)
    assert weekly_report_due(fri.replace(hour=20, minute=59)) is False  # market just closed
    assert weekly_report_due(fri.replace(hour=21)) is True              # 5am MYT
    assert weekly_report_due(fri.replace(hour=23, minute=30)) is True
    sat = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)
    assert weekly_report_due(sat) is True    # catch-up window after a restart
    sun = datetime(2026, 6, 14, 21, 0, tzinfo=timezone.utc)
    assert weekly_report_due(sun) is False
    wed = datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc)
    assert weekly_report_due(wed) is False
