"""M15 conviction layer: scoring, alert routing, sentiment weighting."""

from datetime import datetime, timezone

from narrative_tracker.db import analytics, idempotency, repo
from narrative_tracker.extract.stance import RuleBasedStanceClassifier
from narrative_tracker.notify.telegram_bot import AlertNotifier, build_alert
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.schemas.mention import AssetClass, Mention, ResolutionMethod, Stance
from narrative_tracker.worker import process_post

T0 = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- rule heuristic

async def test_rule_based_conviction_separates_action_from_musing():
    clf = RuleBasedStanceClassifier()
    committed = await clf.classify("added to my $NVDA position, stop at 182, sizing up")
    musing = await clf.classify("$NVDA looks interesting, watching for now, might be a buy")
    assert committed.conviction > 0.7 and committed.is_position is True
    assert musing.conviction < 0.4 and musing.is_position is False
    question = await clf.classify("is $NVDA a buy here?")
    assert question.conviction <= 0.3


def _mention(conviction=0.5, symbol="NVDA"):
    return Mention(symbol=symbol, asset_class=AssetClass.EQUITY,
                   resolution_method=ResolutionMethod.CASHTAG_EXACT,
                   mention_confidence=0.95, stance=Stance.BULLISH,
                   stance_confidence=0.8, conviction=conviction)


def _post(text="$NVDA go", post_id="1"):
    return RawPost(platform_user_id="whale", handle="whale",
                   platform_post_id=post_id, text=text, posted_at=T0)


# --------------------------------------------------------------- alert tags

def test_alert_tags_conviction_extremes():
    strong, _ = build_alert(_post(), _mention(0.9))
    weak, _ = build_alert(_post(), _mention(0.2))
    mid, _ = build_alert(_post(), _mention(0.55))
    assert "\U0001f4aa" in strong          # 💪
    assert "\U0001f4a4" in weak            # 💤
    assert "\U0001f4aa" not in mid and "\U0001f4a4" not in mid


# ------------------------------------------------------------ alert routing

async def test_low_conviction_alerts_go_silent(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory,
                             trading_chat_id=7, silent_below_conviction=0.5)
    assert await notifier.send_alert(_post(post_id="a"), _mention(0.3, "AAA"))
    assert await notifier.send_alert(_post(post_id="b"), _mention(0.8, "BBB"))
    silent, loud = fake_bot.sent[0]["kwargs"], fake_bot.sent[1]["kwargs"]
    assert silent.get("disable_notification") is True
    assert loud.get("disable_notification") is False


async def test_min_conviction_skips_alert_but_not_data(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory,
                             trading_chat_id=7, min_conviction=0.4)
    assert await notifier.send_alert(_post(post_id="c"), _mention(0.2, "CCC")) is False
    assert fake_bot.sent == []


# ------------------------------------------------- end-to-end + sentiment

async def test_process_post_persists_conviction_and_weights_sentiment(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)
    # committed bullish post and a hedged bearish musing on the same ticker
    await process_post(
        RawPost(platform_user_id="a", handle="a", platform_post_id="p1",
                text="added $WINR here, long, sizing up", posted_at=T0),
        session_factory=session_factory, notifier=notifier)
    await process_post(
        RawPost(platform_user_id="b", handle="b", platform_post_id="p2",
                text="$WINR might be weak, watching, could fade", posted_at=T0),
        session_factory=session_factory, notifier=notifier)

    from narrative_tracker.db.models import PostConviction
    from sqlalchemy import select
    async with session_factory() as session:
        rows = (await session.scalars(select(PostConviction))).all()
    assert len(rows) == 2
    assert max(r.conviction for r in rows) > 0.7 and min(r.conviction for r in rows) < 0.4

    # conviction-weighted sentiment: the committed long outweighs the musing short
    detail = await analytics.ticker_detail(session_factory, symbol="WINR", since=T0.replace(hour=0))
    assert detail["sentiment"] > 0
