"""Persistence for the explicit-call ledger (M9-C)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Account, CallScanCursor, ExplicitCall, Post, TickerMention


async def unscanned_posts(sf: async_sessionmaker[AsyncSession], *, limit: int = 40) -> list[dict]:
    """Oldest posts with ticker mentions not yet scanned for explicit calls —
    one mechanism drives both the historical backfill and keeping up live."""
    stmt = (
        select(Post.id, Post.text, Post.posted_at, Post.account_id,
               Post.platform_post_id, Account.handle)
        .join(Account, Post.account_id == Account.id)
        .where(
            exists(select(TickerMention.id).where(TickerMention.post_id == Post.id)),
            ~exists(select(CallScanCursor.post_id).where(CallScanCursor.post_id == Post.id)),
        )
        .order_by(Post.id)
        .limit(limit)
    )
    async with sf() as session:
        return [dict(r._mapping) for r in await session.execute(stmt)]


async def mark_scanned(sf: async_sessionmaker[AsyncSession], post_ids: list[int]) -> None:
    if not post_ids:
        return
    async with sf() as session:
        existing = set(
            await session.scalars(select(CallScanCursor.post_id).where(CallScanCursor.post_id.in_(post_ids)))
        )
        for pid in post_ids:
            if pid not in existing:
                session.add(CallScanCursor(post_id=pid))
        await session.commit()


async def save_call(
    sf: async_sessionmaker[AsyncSession], *, post_id: int, account_id: int, symbol: str,
    direction: str, entry: float | None, stop: float | None, targets: list[float],
    horizon_raw: str | None, horizon_days: int, is_option: bool, confidence: float,
    stated_at: datetime,
) -> bool:
    """Insert if new (idempotent on post+symbol). Returns True when created."""
    async with sf() as session:
        dup = await session.scalar(
            select(ExplicitCall.id).where(ExplicitCall.post_id == post_id, ExplicitCall.symbol == symbol)
        )
        if dup is not None:
            return False
        session.add(ExplicitCall(
            post_id=post_id, account_id=account_id, symbol=symbol, direction=direction,
            entry=entry, stop=stop, targets=targets, horizon_raw=horizon_raw,
            horizon_days=horizon_days, is_option=is_option, confidence=confidence,
            stated_at=stated_at,
        ))
        await session.commit()
        return True


async def open_calls(sf: async_sessionmaker[AsyncSession]) -> list[dict]:
    stmt = (
        select(ExplicitCall, Account.handle)
        .join(Account, ExplicitCall.account_id == Account.id)
        .where(ExplicitCall.status == "open")
        .order_by(ExplicitCall.id)
    )
    async with sf() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {
            "id": c.id, "symbol": c.symbol, "direction": c.direction, "entry": c.entry,
            "stop": c.stop, "targets": c.targets or [], "horizon_days": c.horizon_days,
            "stated_at": c.stated_at, "handle": handle,
        }
        for c, handle in rows
    ]


async def close_call(
    sf: async_sessionmaker[AsyncSession], *, call_id: int, reason: str,
    realized_r: float | None, realized_pct: float | None, closed_at: datetime,
) -> None:
    async with sf() as session:
        row = await session.get(ExplicitCall, call_id)
        if row is None or row.status == "closed":
            return
        row.status = "closed"
        row.close_reason = reason
        row.realized_r = realized_r
        row.realized_pct = realized_pct
        row.closed_at = closed_at
        await session.commit()


async def stated_stats(sf: async_sessionmaker[AsyncSession], *, since: datetime) -> dict[str, dict]:
    """Per-handle stated-call record: closed/open counts, hit rate, averages."""
    stmt = (
        select(ExplicitCall, Account.handle)
        .join(Account, ExplicitCall.account_id == Account.id)
        .where(ExplicitCall.stated_at >= since)
    )
    async with sf() as session:
        rows = (await session.execute(stmt)).all()

    by: dict[str, dict] = defaultdict(lambda: {"open": 0, "closed": 0, "wins": 0, "rs": [], "pcts": []})
    for c, handle in rows:
        s = by[handle]
        if c.status == "closed":
            s["closed"] += 1
            if (c.realized_pct or 0) > 0:
                s["wins"] += 1
            if c.realized_r is not None:
                s["rs"].append(c.realized_r)
            if c.realized_pct is not None:
                s["pcts"].append(c.realized_pct)
        else:
            s["open"] += 1
    out: dict[str, dict] = {}
    for handle, s in by.items():
        out[handle] = {
            "open": s["open"],
            "closed": s["closed"],
            "hit": round(s["wins"] / s["closed"], 3) if s["closed"] else None,
            "avg_r": round(sum(s["rs"]) / len(s["rs"]), 2) if s["rs"] else None,
            "avg_pct": round(sum(s["pcts"]) / len(s["pcts"]), 4) if s["pcts"] else None,
        }
    return out
