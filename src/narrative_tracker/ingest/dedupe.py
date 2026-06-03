"""Post taxonomy, edit/delete handling, and cross-provider dedupe (M1).

* ``classify_post_type`` — original / retweet / quote / reply (different signals).
* ``is_edited`` — detect an edited post via ``edit_history_tweet_ids`` length.
* ``tombstone_post`` — mark a deleted post (sets ``deleted_at`` + state); the
  worker uses this to retract derived alerts later.
* ``is_near_duplicate`` — same author + same normalized content within a short
  window. Catches the same tweet arriving from the primary and fallback feed with
  different ids (the (account, platform_post_id) unique key can't see that).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..text_utils import content_sha
from ..db.models import Post


def classify_post_type(
    *, is_retweet: bool = False, is_quote: bool = False, is_reply: bool = False
) -> str:
    if is_retweet:
        return "retweet"
    if is_quote:
        return "quote"
    if is_reply:
        return "reply"
    return "original"


def is_edited(edit_history_tweet_ids: list | None) -> bool:
    """X exposes ``edit_history_tweet_ids``; length > 1 means the post was edited."""
    return bool(edit_history_tweet_ids) and len(edit_history_tweet_ids) > 1


async def is_near_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    text: str,
    posted_at: datetime,
    window_s: int = 120,
) -> bool:
    """True if the same author already has a post with identical normalized
    content within ``window_s`` seconds (cross-provider / repost guard)."""
    sha = content_sha(text)
    lo = posted_at - timedelta(seconds=window_s)
    hi = posted_at + timedelta(seconds=window_s)
    async with session_factory() as session:
        existing = await session.scalar(
            select(Post.id).where(
                Post.account_id == account_id,
                Post.content_sha == sha,
                Post.posted_at.between(lo, hi),
            )
        )
    return existing is not None


async def tombstone_post(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    platform_post_id: str,
) -> bool:
    """Mark a deleted post as tombstoned. Returns True if a row was updated."""
    async with session_factory() as session:
        result = await session.execute(
            update(Post)
            .where(
                Post.account_id == account_id,
                Post.platform_post_id == platform_post_id,
            )
            .values(state="tombstoned", deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return result.rowcount > 0
