"""Source-provider contract.

A provider yields :class:`RawPost` events. The real one (twitterapi.io stream)
and the test ``FakeProvider`` both satisfy :class:`SourceProvider`, so the worker
spine is agnostic to where posts come from — and a fallback vendor can be swapped
in later without touching downstream code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(slots=True)
class RawPost:
    """A normalized inbound post, provider-agnostic."""

    platform_user_id: str          # stable numeric account id (NOT the handle)
    handle: str
    platform_post_id: str          # stable post id -> dedupe key with the account
    text: str
    posted_at: datetime
    post_type: str = "original"    # original|retweet|quote|reply
    media_urls: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)  # at-ingest engagement: likes/retweets/replies/views
    raw: dict = field(default_factory=dict)


@runtime_checkable
class SourceProvider(Protocol):
    """Anything that can stream :class:`RawPost` events."""

    def stream(self) -> AsyncIterator[RawPost]:
        """Yield posts as they arrive (an async iterator)."""
        ...
