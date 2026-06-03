"""twitterapi.io WebSocket stream client + a FakeProvider for tests/local.

The real client pushes posts in ~1s (the plan's <60s budget is comfortable). It
reconnects with exponential backoff + jitter and is resilient to transient drops.

NOTE: the exact stream payload schema (esp. the media-URL field) must be verified
against a live sample before trusting the vision path downstream — see the plan's
"Validation TODO". ``_parse_message`` is intentionally small and tolerant so that
mapping is easy to adjust.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable, Sequence

from .provider import RawPost

log = logging.getLogger(__name__)


class TwitterApiIoStreamClient:
    """Streams posts for a set of account ids from twitterapi.io.

    Requires an API key; raises if constructed without one when ``stream()`` is
    awaited. The ``websockets`` dependency is imported lazily (part of the
    ``prod`` extra) so importing this module never requires it.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        ws_url: str,
        account_ids: Sequence[str],
        max_backoff_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._ws_url = ws_url
        self._account_ids = list(account_ids)
        self._max_backoff_s = max_backoff_s

    async def stream(self) -> AsyncIterator[RawPost]:
        if not self._api_key:
            raise RuntimeError(
                "twitterapi.io API key not configured (set NT_TWITTERAPI_IO_KEY)"
            )
        try:
            import websockets  # lazy: part of the `prod` extra
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "websockets not installed; `pip install -e '.[prod]'`"
            ) from exc

        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers={"x-api-key": self._api_key},
                ) as ws:
                    await ws.send(
                        json.dumps({"action": "subscribe", "accounts": self._account_ids})
                    )
                    attempt = 0  # reset backoff on a successful connect
                    async for message in ws:
                        post = self._parse_message(message)
                        if post is not None:
                            yield post
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                attempt += 1
                delay = min(self._max_backoff_s, 2 ** attempt)
                # full jitter
                delay = delay * (0.5 + 0.5 * _jitter())
                log.warning("stream dropped (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    @staticmethod
    def _parse_message(message: str | bytes) -> RawPost | None:
        """Map a raw stream frame to a :class:`RawPost`. Tolerant of unknown
        frames (heartbeats, acks) -> returns ``None`` to skip them."""
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return None
        tweet = data.get("tweet") or data.get("data") or data
        post_id = tweet.get("id") or tweet.get("id_str")
        author = tweet.get("author") or tweet.get("user") or {}
        user_id = author.get("id") or author.get("id_str")
        if not post_id or not user_id:
            return None
        media = [
            m.get("media_url_https") or m.get("media_url")
            for m in (tweet.get("extended_entities", {}).get("media", []) or [])
            if m.get("media_url_https") or m.get("media_url")
        ]
        return RawPost(
            platform_user_id=str(user_id),
            handle=author.get("userName") or author.get("screen_name") or "",
            platform_post_id=str(post_id),
            text=tweet.get("text") or tweet.get("full_text") or "",
            posted_at=_parse_ts(tweet.get("createdAt") or tweet.get("created_at")),
            post_type="retweet" if tweet.get("retweeted_status") else "original",
            media_urls=media,
            raw=tweet,
        )


class FakeProvider:
    """A provider that replays a fixed list of posts. For tests and local demos."""

    def __init__(self, posts: Iterable[RawPost], *, delay_s: float = 0.0) -> None:
        self._posts = list(posts)
        self._delay_s = delay_s

    async def stream(self) -> AsyncIterator[RawPost]:
        for post in self._posts:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield post


def _jitter() -> float:
    # Deterministic-enough jitter without importing random (which is fine here,
    # but we avoid it to keep the module import-pure). Uses event-loop time.
    return (asyncio.get_event_loop().time() % 1.0)


def _parse_ts(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)
