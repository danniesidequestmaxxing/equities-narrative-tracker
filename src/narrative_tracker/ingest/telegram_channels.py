"""Public Telegram channel ingestion (M13, path 2).

Public channels expose a web preview at ``https://t.me/s/<name>`` — pollable
with NO credentials, no API costs, and no account to get rate-limited. We poll
it like the X feed, parse the message blocks, and emit the same ``RawPost``
the rest of the pipeline already understands.

Source convention: ``platform_user_id = handle = "tg:<name>"`` — one namespace
with X handles, filtered by prefix at the pollers. Message links become
``https://t.me/<name>/<id>`` receipts; photos ride the existing vision path;
forwards are tagged like retweets.

Parsing is regex-over-stable-markup (no HTML-parser dependency) and fully
covered by a recorded-fixture test, mirroring the twitterapi.io provider.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable

from .provider import RawPost

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[str]]

_BLOCK_RE = re.compile(r'data-post="(?P<post>[^"]+)"(?P<body>.*?)(?=data-post="|\Z)', re.DOTALL)
_TEXT_RE = re.compile(r'tgme_widget_message_text[^>]*>(?P<text>.*?)</div>', re.DOTALL)
_TIME_RE = re.compile(r'<time datetime="(?P<dt>[^"]+)"')
_PHOTO_RE = re.compile(r"background-image:url\('(?P<url>[^']+)'\)")
_VIEWS_RE = re.compile(r'tgme_widget_message_views[^>]*>(?P<views>[^<]+)<')
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _strip_html(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub("", text)
    return html_mod.unescape(text).strip()


def _parse_views(raw: str) -> int:
    raw = raw.strip().upper().replace(",", "")
    try:
        if raw.endswith("K"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("M"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(float(raw))
    except ValueError:
        return 0


def parse_channel_page(html: str, channel: str) -> list[dict]:
    """Message dicts (ascending id) from a t.me/s/<channel> page."""
    out: list[dict] = []
    for m in _BLOCK_RE.finditer(html):
        post_ref = m.group("post")          # "channel/1234"
        body = m.group("body")
        if "/" not in post_ref:
            continue
        msg_id = post_ref.rsplit("/", 1)[1]
        if not msg_id.isdigit():
            continue
        text_m = _TEXT_RE.search(body)
        text = _strip_html(text_m.group("text")) if text_m else ""
        time_m = _TIME_RE.search(body)
        if not time_m:
            continue
        posted_at = datetime.fromisoformat(time_m.group("dt")).astimezone(timezone.utc)
        photos = [u for u in _PHOTO_RE.findall(body) if u.startswith("http")]
        views_m = _VIEWS_RE.search(body)
        out.append({
            "id": int(msg_id),
            "text": text,
            "posted_at": posted_at,
            "photos": photos,
            "views": _parse_views(views_m.group("views")) if views_m else 0,
            "forwarded": "tgme_widget_message_forwarded_from" in body,
        })
    out.sort(key=lambda d: d["id"])
    return out


def _to_rawpost(msg: dict, channel: str) -> RawPost:
    return RawPost(
        platform_user_id=f"tg:{channel.lower()}",
        handle=f"tg:{channel.lower()}",
        platform_post_id=str(msg["id"]),
        text=msg["text"],
        posted_at=msg["posted_at"],
        post_type="forward" if msg["forwarded"] else "original",
        media_urls=msg["photos"],
        metrics={"views": msg["views"]} if msg["views"] else {},
    )


class TgPublicChannelProvider:
    """Polls public-channel previews; channels come from the dynamic watchlist
    (``tg:``-prefixed sources), so /addchannel goes live within one cycle."""

    def __init__(
        self,
        *,
        channels_provider: Callable[[], Awaitable[list[str]]],
        poll_interval_s: int = 120,
        initial_lookback_s: int = 3600,
        fetch: Fetch | None = None,
    ) -> None:
        self._channels_provider = channels_provider
        self._interval = poll_interval_s
        self._lookback = initial_lookback_s
        self._fetch = fetch
        self._since: dict[str, int] = {}  # channel -> last seen message id

    async def _poll_channel(self, channel: str, fetch: Fetch) -> AsyncIterator[RawPost]:
        try:
            page = await fetch(f"https://t.me/s/{channel}")
        except Exception as exc:  # noqa: BLE001 - one channel must not kill the loop
            log.warning("tg poll failed for %s: %s", channel, exc)
            return
        msgs = parse_channel_page(page, channel)
        if not msgs:
            return
        last = self._since.get(channel)
        if last is None:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._lookback)
            fresh = [m for m in msgs if m["posted_at"] >= cutoff]
        else:
            fresh = [m for m in msgs if m["id"] > last]
        self._since[channel] = msgs[-1]["id"]
        for m in fresh:
            yield _to_rawpost(m, channel)

    async def stream(self) -> AsyncIterator[RawPost]:
        fetch = self._fetch or _http_fetch
        logged: list[str] | None = None
        while True:
            channels = [c.removeprefix("tg:") for c in await self._channels_provider()]
            if channels != logged:
                log.info("tg channels: %d: %s", len(channels), ", ".join(channels) or "—")
                logged = channels
            for ch in channels:
                async for post in self._poll_channel(ch, fetch):
                    yield post
            await asyncio.sleep(self._interval)


async def _http_fetch(url: str) -> str:  # pragma: no cover - prod path
    import httpx  # lazy: part of the `prod` extra

    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (narrative-tracker)"}, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
