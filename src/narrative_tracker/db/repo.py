"""Small repository helpers (accounts, mentions, audit) used by the worker."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Account, AccountScore, AuditLog, Post, TickerMention


async def get_or_create_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform_user_id: str,
    handle: str,
    tier: str = "COLD",
) -> int:
    """Return the account id for a platform user, creating it if needed.

    Keyed on the stable numeric ``platform_user_id`` (handles can change).
    """
    async with session_factory() as session:
        existing = await session.scalar(
            select(Account.id).where(Account.platform_user_id == platform_user_id)
        )
        if existing is not None:
            return existing
        account = Account(platform_user_id=platform_user_id, handle=handle, tier=tier)
        session.add(account)
        try:
            await session.commit()
            return account.id
        except IntegrityError:
            # Concurrent create — fetch the winner.
            await session.rollback()
            return await session.scalar(
                select(Account.id).where(
                    Account.platform_user_id == platform_user_id
                )
            )


async def add_mentions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    post_id: int,
    mentions: list[dict],
) -> list[int]:
    """Persist extracted mentions for a post; returns their ids."""
    if not mentions:
        return []
    async with session_factory() as session:
        rows = [
            TickerMention(
                post_id=post_id,
                symbol=m["symbol"],
                asset_class=m.get("asset_class", "equity"),
                resolution_method=m.get("resolution_method", "cashtag_exact"),
                mention_confidence=m.get("mention_confidence", 1.0),
                stance=m.get("stance", "neutral"),
                negation_flag=m.get("negation_flag", False),
                stance_confidence=m.get("stance_confidence", 0.0),
                option_detail=m.get("option_detail"),
            )
            for m in mentions
        ]
        session.add_all(rows)
        await session.commit()
        return [r.id for r in rows]


async def set_post_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    post_id: int,
    state: str,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Post).where(Post.id == post_id).values(state=state)
        )
        await session.commit()


async def record_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    payload: dict,
) -> None:
    async with session_factory() as session:
        session.add(AuditLog(event_type=event_type, payload=payload))
        await session.commit()


async def insert_account_score(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    as_of: datetime,
    decayed_score: float,
    sample_size: int,
    accuracy: float = 0.0,
    max_closed_at: datetime | None = None,
) -> None:
    """Append a point-in-time credibility row (INV-3)."""
    async with session_factory() as session:
        session.add(
            AccountScore(
                account_id=account_id,
                as_of=as_of,
                accuracy=accuracy,
                sample_size=sample_size,
                decayed_score=decayed_score,
                max_closed_at=max_closed_at,
            )
        )
        await session.commit()


async def get_credibility(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    as_of: datetime,
) -> float:
    """Latest credibility as-of ``as_of``; falls back to the tier prior."""
    from ..analyze.sentiment import credibility_prior

    async with session_factory() as session:
        row = await session.scalar(
            select(AccountScore)
            .where(AccountScore.account_id == account_id, AccountScore.as_of <= as_of)
            .order_by(AccountScore.as_of.desc())
            .limit(1)
        )
        if row is not None:
            return row.decayed_score
        tier = await session.scalar(select(Account.tier).where(Account.id == account_id))
    return credibility_prior(tier or "COLD")
