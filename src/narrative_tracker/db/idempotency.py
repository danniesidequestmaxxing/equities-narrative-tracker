"""Idempotency ledger + post dedupe.

The two M0 correctness guarantees both rest on **Postgres unique constraints**
(which SQLite honors identically), not on application-level checks:

* ``insert_post_if_new`` — dedupe on (account_id, platform_post_id). A duplicate
  post (provider redelivery, overlapping fallback feed, worker restart) inserts
  once.
* ``claim_send`` / ``mark_sent`` — the ``sent_messages`` ledger. ``claim_send``
  INSERTs a ``pending`` row **before** the Telegram call; if the unique
  ``idempotency_key`` already exists it returns ``False`` and the caller does not
  send. This is *claim-before-send*: a crash between claim and send risks a
  missed alert (never a duplicate), which is the safe direction for trade calls.

Keys are derived deterministically from the source event — never from
``now()``/UUID — so a replay produces the same key and dedupes correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..text_utils import content_sha
from .models import Post, SentMessage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def alert_idempotency_key(
    platform_user_id: str, platform_post_id: str, symbol: str
) -> str:
    """Deterministic key for a per-(post, symbol) alert. Derived from the source
    event so it is stable across restarts and replays."""
    return f"ALERT:{platform_user_id}:{platform_post_id}:{symbol.upper()}"


async def insert_post_if_new(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    platform_post_id: str,
    text: str,
    posted_at: datetime,
    post_type: str = "original",
) -> tuple[int | None, bool]:
    """Insert a post, deduped on (account_id, platform_post_id).

    Returns ``(post_id, is_new)``. On a duplicate, returns the existing id and
    ``False`` — the unique constraint is the authority.
    """
    async with session_factory() as session:
        post = Post(
            account_id=account_id,
            platform_post_id=platform_post_id,
            text=text,
            posted_at=posted_at,
            post_type=post_type,
            content_sha=content_sha(text),
        )
        session.add(post)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(Post.id).where(
                    Post.account_id == account_id,
                    Post.platform_post_id == platform_post_id,
                )
            )
            return existing, False
        return post.id, True


async def claim_send(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: str,
    chat_id: int,
) -> bool:
    """Claim the right to send a message. Returns ``True`` if newly claimed (the
    caller should send), ``False`` if already claimed (do not send).

    The INSERT against the unique ``idempotency_key`` is the authority — this is
    safe under concurrency and across worker restarts.
    """
    async with session_factory() as session:
        session.add(
            SentMessage(idempotency_key=idempotency_key, chat_id=chat_id, status="pending")
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def mark_sent(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: str,
    telegram_message_id: int,
) -> None:
    """Record a successful send: flip ``pending`` -> ``sent`` and store the
    Telegram ``message_id`` (needed later for retractions)."""
    async with session_factory() as session:
        await session.execute(
            update(SentMessage)
            .where(SentMessage.idempotency_key == idempotency_key)
            .values(
                status="sent",
                telegram_message_id=telegram_message_id,
                sent_at=_utcnow(),
            )
        )
        await session.commit()
