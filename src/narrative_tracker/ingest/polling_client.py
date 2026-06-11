"""twitterapi.io polling provider (real-time monitoring is poll-based, not WS).

Polls ``/twitter/tweet/advanced_search`` per watched handle with a moving
``since_time`` window. Conforms to :class:`SourceProvider`. HTTP is injected so
parsing is testable against recorded JSON; the real client uses httpx (lazy).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from .provider import RawPost

log = logging.getLogger(__name__)

Fetch = Callable[[str, dict, dict], Awaitable[dict]]


def _parse_ts(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def _to_rawpost(tweet: dict) -> RawPost | None:
    author = tweet.get("author") or tweet.get("user") or {}
    post_id = tweet.get("id") or tweet.get("id_str") or tweet.get("tweet_id")
    user_id = author.get("id") or author.get("id_str") or author.get("userId")
    if not post_id:
        return None
    handle = author.get("userName") or author.get("screen_name") or author.get("username") or ""
    media = [
        m.get("media_url_https") or m.get("media_url")
        for m in (tweet.get("extendedEntities", {}) or tweet.get("extended_entities", {})).get("media", []) or []
        if m.get("media_url_https") or m.get("media_url")
    ]
    text = tweet.get("text") or tweet.get("full_text") or ""
    if tweet.get("retweeted_tweet") or tweet.get("retweeted_status"):
        post_type = "retweet"
    elif (
        tweet.get("isReply")
        or tweet.get("inReplyToId")
        or tweet.get("in_reply_to_status_id")
        or tweet.get("in_reply_to_status_id_str")
        or text.startswith("@")
    ):
        # Replies don't show on the profile timeline — tag them so alerts can say so.
        post_type = "reply"
    else:
        post_type = "original"
    # Key accounts by handle (lowercased): it's what /addsource, the poller query,
    # and the tweet author all share — keeps tier + credibility coherent.
    return RawPost(
        platform_user_id=str(handle or user_id or "unknown").lower(),
        handle=handle,
        platform_post_id=str(post_id),
        text=text,
        posted_at=_parse_ts(tweet.get("createdAt") or tweet.get("created_at")),
        post_type=post_type,
        media_urls=media,
        raw=tweet,
    )


def _tweets_of(payload: dict) -> list[dict]:
    for key in ("tweets", "data", "results"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


class TwitterApiIoPollingProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        handles: list[str] | None = None,
        handles_provider: Callable[[], Awaitable[list[str]]] | None = None,
        base_url: str = "https://api.twitterapi.io",
        poll_interval_s: int = 120,
        initial_lookback_s: int = 3600,
        fetch: Fetch | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._static = [h.lstrip("@") for h in (handles or []) if h.strip()]
        self._handles_provider = handles_provider
        self._base = base_url
        self._interval = poll_interval_s
        self._lookback = initial_lookback_s
        self._fetch = fetch
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._since: dict[str, int] = {}
        self._logged_shape = False

    async def _current_handles(self) -> list[str]:
        if self._handles_provider is not None:
            return [h.lstrip("@") for h in (await self._handles_provider()) if h.strip()]
        return self._static

    async def stream(self) -> AsyncIterator[RawPost]:
        if not self._api_key:
            raise RuntimeError("twitterapi.io API key not configured (NT_TWITTERAPI_IO_KEY)")
        fetch = self._fetch or self._http_fetch
        last_logged: list[str] | None = None
        while True:
            handles = await self._current_handles()
            if handles != last_logged:  # log only when the watchlist changes
                log.info("watchlist: %d accounts: %s", len(handles), ", ".join(handles) or "(empty)")
                last_logged = handles
            for handle in handles:
                try:
                    async for post in self._poll_handle(handle, fetch):
                        yield post
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad handle must not stop the loop
                    log.warning("poll @%s failed: %s", handle, exc)
            await asyncio.sleep(self._interval)

    async def _poll_handle(self, handle: str, fetch: Fetch) -> AsyncIterator[RawPost]:
        now_unix = int(self._now().timestamp())
        since = self._since.get(handle, now_unix - self._lookback)
        query = f"from:{handle} since_time:{since} until_time:{now_unix}"
        cursor = None
        max_seen = since
        headers = {"X-API-Key": self._api_key}
        for _page in range(10):  # safety cap on pagination
            params = {"query": query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            data = await fetch("/twitter/tweet/advanced_search", params, headers)
            if not self._logged_shape:
                log.info("twitterapi response keys: %s", list(data.keys()))
                self._logged_shape = True
            for tweet in _tweets_of(data):
                post = _to_rawpost(tweet)
                if post is not None:
                    max_seen = max(max_seen, int(post.posted_at.timestamp()))
                    yield post
            if data.get("has_next_page") and data.get("next_cursor"):
                cursor = data["next_cursor"]
            else:
                break
        self._since[handle] = max_seen + 1

    async def _http_fetch(self, path: str, params: dict, headers: dict) -> dict:  # pragma: no cover
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(self._base + path, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
