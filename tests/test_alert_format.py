"""Alert receipts: every alert quotes the post text + deep-links the exact post."""

from datetime import datetime, timezone

from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.notify.telegram_bot import build_alert, post_url
from narrative_tracker.schemas.mention import AssetClass, Mention, ResolutionMethod, Stance


def _post(text, *, post_type="original", post_id="2064943854068060238", handle="aleabitoreddit"):
    return RawPost(
        platform_user_id=handle, handle=handle, platform_post_id=post_id, text=text,
        posted_at=datetime(2026, 6, 11, 5, 32, tzinfo=timezone.utc), post_type=post_type,
    )


def _mention(symbol="RDDT"):
    return Mention(symbol=symbol, asset_class=AssetClass.EQUITY,
                   resolution_method=ResolutionMethod.CASHTAG_EXACT,
                   mention_confidence=0.95, stance=Stance.NEUTRAL, stance_confidence=0.6)


def test_alert_quotes_text_and_deep_links_post():
    post = _post("@Ud197601 Everyone kept calling $AXTI a scam, and my thesis BS on $RDDT.",
                 post_type="reply")
    mdv2, plain = build_alert(post, _mention())
    deep = "https://x.com/aleabitoreddit/status/2064943854068060238"
    assert deep in mdv2 and deep in plain
    assert "thesis BS on" in plain          # the receipt: post text is quoted
    assert "reply" in mdv2 and "reply" in plain  # hidden-from-timeline marker


def test_alert_truncates_long_text():
    post = _post("$NVDA " + "very long take " * 40)
    mdv2, plain = build_alert(post, _mention("NVDA"))
    assert "…" in plain and len(plain) < 600


def test_post_url_falls_back_to_profile():
    post = _post("$NVDA", post_id="")
    assert post_url(post) == "https://x.com/aleabitoreddit"


def test_watched_ticker_gets_bell():
    post = _post("$NVDA breaking out")
    mdv2, plain = build_alert(post, _mention("NVDA"), watched=True)
    assert mdv2.startswith("🔔") and "[WATCHED]" in plain
    mdv2_un, plain_un = build_alert(post, _mention("NVDA"))
    assert not mdv2_un.startswith("🔔") and "[WATCHED]" not in plain_un
