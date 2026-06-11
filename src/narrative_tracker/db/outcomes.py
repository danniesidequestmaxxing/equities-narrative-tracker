"""Persistence for mention outcomes (M9 event-study rows)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Account, MentionOutcome, Post, TickerMention


async def mentions_needing_outcomes(
    sf: async_sessionmaker[AsyncSession], *, since: datetime
) -> list[dict]:
    """Equity mentions in the window with no outcome row yet, or an incomplete
    one (fwd_5d still null) — the nightly job's work list."""
    stmt = (
        select(
            TickerMention.id.label("mention_id"),
            TickerMention.symbol,
            TickerMention.stance,
            Post.posted_at,
            Post.account_id,
        )
        .join(Post, TickerMention.post_id == Post.id)
        .outerjoin(MentionOutcome, MentionOutcome.mention_id == TickerMention.id)
        .where(
            Post.posted_at >= since,
            TickerMention.asset_class == "equity",
            (MentionOutcome.id.is_(None)) | (MentionOutcome.fwd_5d.is_(None)),
        )
        .order_by(Post.posted_at)
    )
    async with sf() as session:
        return [dict(r._mapping) for r in await session.execute(stmt)]


async def upsert_outcome(
    sf: async_sessionmaker[AsyncSession],
    *,
    mention_id: int,
    account_id: int,
    symbol: str,
    stance: str,
    posted_at: datetime,
    px_post: float,
    fwd: dict,
    bench: dict | None,
) -> None:
    async with sf() as session:
        row = await session.scalar(
            select(MentionOutcome).where(MentionOutcome.mention_id == mention_id)
        )
        if row is None:
            row = MentionOutcome(
                mention_id=mention_id, account_id=account_id, symbol=symbol,
                stance=stance, posted_at=posted_at, px_post=px_post,
            )
            session.add(row)
        row.px_post = px_post
        row.fwd_1d, row.fwd_3d, row.fwd_5d = fwd.get(1), fwd.get(3), fwd.get(5)
        if bench:
            row.bench_1d, row.bench_3d, row.bench_5d = bench.get(1), bench.get(3), bench.get(5)
        await session.commit()


async def outcomes_for_accounts(
    sf: async_sessionmaker[AsyncSession], *, since: datetime
) -> list[dict]:
    """All outcome rows in the window joined with handle/tier — scoreboard input."""
    stmt = (
        select(
            MentionOutcome.symbol, MentionOutcome.stance, MentionOutcome.posted_at,
            MentionOutcome.fwd_1d, MentionOutcome.fwd_3d, MentionOutcome.fwd_5d,
            MentionOutcome.bench_1d, MentionOutcome.bench_3d, MentionOutcome.bench_5d,
            MentionOutcome.account_id, Account.handle, Account.tier,
        )
        .join(Account, MentionOutcome.account_id == Account.id)
        .where(MentionOutcome.posted_at >= since)
        .order_by(MentionOutcome.posted_at.desc())
    )
    async with sf() as session:
        return [dict(r._mapping) for r in await session.execute(stmt)]
