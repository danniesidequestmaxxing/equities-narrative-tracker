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


async def active_handles(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Distinct handles of active watched accounts (drives the poller)."""
    async with session_factory() as session:
        rows = await session.scalars(select(Account.handle).where(Account.active.is_(True)))
    out: list[str] = []
    seen: set[str] = set()
    for h in rows:
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


async def get_account_id(
    session_factory: async_sessionmaker[AsyncSession], *, platform_user_id: str
) -> int | None:
    async with session_factory() as session:
        return await session.scalar(
            select(Account.id).where(Account.platform_user_id == platform_user_id)
        )


async def save_engagement(
    session_factory: async_sessionmaker[AsyncSession], *, post_id: int, metrics: dict
) -> None:
    """At-ingest engagement snapshot (idempotent on post_id)."""
    from .models import PostEngagement

    async with session_factory() as session:
        if await session.get(PostEngagement, post_id) is not None:
            return
        session.add(PostEngagement(
            post_id=post_id,
            likes=int(metrics.get("likes", 0)),
            retweets=int(metrics.get("retweets", 0)),
            replies=int(metrics.get("replies", 0)),
            views=int(metrics.get("views", 0)),
        ))
        await session.commit()


async def last_stance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    symbol: str,
    before: datetime,
    within_days: int = 21,
) -> dict | None:
    """The account's most recent directional take on a symbol before ``before``
    (reversal detection input). Only bullish/bearish count — neutral isn't a
    position to reverse from."""
    from datetime import timedelta

    from .models import Post, TickerMention

    stmt = (
        select(TickerMention.stance, TickerMention.stance_confidence, Post.posted_at)
        .join(Post, TickerMention.post_id == Post.id)
        .where(
            Post.account_id == account_id,
            TickerMention.symbol == symbol,
            TickerMention.stance.in_(("bullish", "bearish")),
            Post.posted_at < before,
            Post.posted_at >= before - timedelta(days=within_days),
        )
        .order_by(Post.posted_at.desc())
        .limit(1)
    )
    async with session_factory() as session:
        row = (await session.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def latest_account_scores(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[int, float]:
    """Latest credibility score per account (evidence-weighted when M9 stats
    exist). Small table — fetch ordered, last write wins."""
    from .models import AccountScore

    async with session_factory() as session:
        rows = await session.execute(
            select(AccountScore.account_id, AccountScore.decayed_score).order_by(AccountScore.as_of)
        )
        out: dict[int, float] = {}
        for account_id, score in rows:
            out[account_id] = float(score)
        return out


async def all_accounts(session_factory: async_sessionmaker[AsyncSession]) -> list[dict]:
    async with session_factory() as session:
        rows = await session.scalars(select(Account))
        return [{"id": a.id, "handle": a.handle, "tier": a.tier} for a in rows]


# --- per-ticker watchlist (🔔 alerts + guaranteed pulse deep-dive) -----------


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().lstrip("$").upper()


async def watch_ticker(session_factory: async_sessionmaker[AsyncSession], symbol: str) -> str:
    from .models import WatchedTicker

    sym = _norm_symbol(symbol)
    async with session_factory() as session:
        row = await session.scalar(select(WatchedTicker).where(WatchedTicker.symbol == sym))
        if row is None:
            session.add(WatchedTicker(symbol=sym, active=True))
        else:
            row.active = True
        await session.commit()
    return sym


async def unwatch_ticker(session_factory: async_sessionmaker[AsyncSession], symbol: str) -> bool:
    from .models import WatchedTicker

    sym = _norm_symbol(symbol)
    async with session_factory() as session:
        row = await session.scalar(select(WatchedTicker).where(WatchedTicker.symbol == sym))
        if row is None or not row.active:
            return False
        row.active = False
        await session.commit()
    return True


async def watched_tickers(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    from .models import WatchedTicker

    async with session_factory() as session:
        rows = await session.scalars(
            select(WatchedTicker.symbol).where(WatchedTicker.active.is_(True)).order_by(WatchedTicker.id)
        )
        return list(rows)


async def is_watched(session_factory: async_sessionmaker[AsyncSession], symbol: str) -> bool:
    from .models import WatchedTicker

    async with session_factory() as session:
        return bool(
            await session.scalar(
                select(WatchedTicker.id).where(
                    WatchedTicker.symbol == _norm_symbol(symbol), WatchedTicker.active.is_(True)
                )
            )
        )


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
