"""24h operational snapshot (M12) — the numbers behind the daily ops line."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    Account, ExplicitCall, MentionOutcome, Post, SentMessage, TickerMention,
)


async def snapshot(sf: async_sessionmaker[AsyncSession], *, since: datetime) -> dict:
    async with sf() as session:
        posts = await session.scalar(
            select(func.count(Post.id)).where(Post.ingested_at >= since)
        )
        mentions = await session.scalar(
            select(func.count(TickerMention.id))
            .join(Post, TickerMention.post_id == Post.id)
            .where(Post.ingested_at >= since)
        )
        sent = await session.scalar(
            select(func.count(SentMessage.id)).where(
                SentMessage.created_at >= since, SentMessage.status == "sent"
            )
        )
        outcomes = await session.scalar(
            select(func.count(MentionOutcome.id)).where(MentionOutcome.computed_at >= since)
        )
        stated = await session.scalar(
            select(func.count(ExplicitCall.id)).where(ExplicitCall.created_at >= since)
        )
        accounts = await session.scalar(
            select(func.count(Account.id)).where(Account.active.is_(True))
        )
    return {
        "posts": int(posts or 0),
        "mentions": int(mentions or 0),
        "sent": int(sent or 0),
        "outcomes": int(outcomes or 0),
        "stated": int(stated or 0),
        "accounts": int(accounts or 0),
    }
