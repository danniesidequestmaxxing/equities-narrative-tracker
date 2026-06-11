"""M13: public Telegram channel ingestion + composite stream + tg: plumbing."""

import asyncio
from datetime import datetime, timedelta, timezone

from narrative_tracker.admin.commands import handle_command
from narrative_tracker.db import repo
from narrative_tracker.ingest.composite import CompositeProvider
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.ingest.telegram_channels import TgPublicChannelProvider, parse_channel_page
from narrative_tracker.notify.telegram_bot import post_url

ADMIN = [999]
NOW = datetime.now(timezone.utc)
T1 = (NOW - timedelta(minutes=30)).isoformat()
T2 = (NOW - timedelta(minutes=10)).isoformat()

PAGE = f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message" data-post="alphachan/101">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Long <b>$WINR</b> here, stop 9 &amp; target 12<br/>swing setup</div>
  <span class="tgme_widget_message_views">12.3K</span>
  <time datetime="{T1}" class="time">13:13</time>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message" data-post="alphachan/102">
  <div class="tgme_widget_message_forwarded_from">Forwarded from Someone</div>
  <a class="tgme_widget_message_photo_wrap" style="width:480px;background-image:url('https://cdn4.telesco.pe/file/chart.jpg')"></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto">$MRVL chart 👇</div>
  <span class="tgme_widget_message_views">980</span>
  <time datetime="{T2}" class="time">13:33</time>
 </div>
</div>
"""


def test_parse_channel_page_extracts_messages():
    msgs = parse_channel_page(PAGE, "alphachan")
    assert [m["id"] for m in msgs] == [101, 102]
    m1, m2 = msgs
    assert "Long $WINR here, stop 9 & target 12" in m1["text"] and "swing setup" in m1["text"]
    assert m1["views"] == 12300 and m1["forwarded"] is False
    assert m2["photos"] == ["https://cdn4.telesco.pe/file/chart.jpg"]
    assert m2["forwarded"] is True
    assert m1["posted_at"].tzinfo is not None


async def test_provider_polls_and_dedupes_by_message_id():
    pages = {"https://t.me/s/alphachan": PAGE}
    calls = []

    async def fetch(url):
        calls.append(url)
        return pages[url]

    async def channels():
        return ["tg:alphachan"]

    prov = TgPublicChannelProvider(channels_provider=channels, fetch=fetch, initial_lookback_s=3600)
    out = []
    async for p in prov._poll_channel("alphachan", fetch):
        out.append(p)
    assert [p.platform_post_id for p in out] == ["101", "102"]
    assert out[0].platform_user_id == "tg:alphachan" and out[0].handle == "tg:alphachan"
    assert out[1].post_type == "forward" and out[1].media_urls  # photo rides the vision path
    assert out[0].metrics == {"views": 12300}

    again = [p async for p in prov._poll_channel("alphachan", fetch)]
    assert again == []  # same page -> nothing new


async def test_composite_merges_streams():
    class One:
        async def stream(self):
            yield RawPost(platform_user_id="a", handle="a", platform_post_id="1",
                          text="$A", posted_at=NOW)

    class Two:
        async def stream(self):
            yield RawPost(platform_user_id="tg:b", handle="tg:b", platform_post_id="2",
                          text="$B", posted_at=NOW)

    comp = CompositeProvider([One(), Two()])
    seen = []
    async for post in comp.stream():
        seen.append(post.platform_user_id)
        if len(seen) == 2:
            break
    assert set(seen) == {"a", "tg:b"}


def test_post_url_handles_telegram_sources():
    tg = RawPost(platform_user_id="tg:alphachan", handle="tg:alphachan",
                 platform_post_id="102", text="x", posted_at=NOW)
    assert post_url(tg) == "https://t.me/alphachan/102"
    x = RawPost(platform_user_id="whale", handle="whale", platform_post_id="9",
                text="x", posted_at=NOW)
    assert post_url(x) == "https://x.com/whale/status/9"


async def test_addchannel_and_tme_paste(session_factory):
    r = await handle_command("/addchannel AlphaChan hot", 999, session_factory, ADMIN)
    assert "t.me/alphachan" in r and "HOT" in r
    assert "tg:alphachan" in await repo.active_handles(session_factory)

    r2 = await handle_command("https://t.me/another_one", 999, session_factory, ADMIN)
    assert "t.me/another_one" in r2
    handles = await repo.active_handles(session_factory)
    assert "tg:another_one" in handles

    # the X poller must never see tg: sources
    x_side = [h for h in handles if not h.startswith("tg:")]
    assert x_side == []
