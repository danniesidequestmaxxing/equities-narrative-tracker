"""Budget ledger + circuit breaker (M5).

Durable, idempotent (per ``ref``) spend tracking in Postgres. ``charge`` records a
cost and returns whether the bucket is still under its cap; ``over_budget`` is the
read used by the gate chain. Postgres is the authority; the LiteLLM gateway is the
hard cap in production.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import BudgetLedger


async def spent(sf: async_sessionmaker[AsyncSession], bucket: str) -> float:
    async with sf() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(BudgetLedger.amount), 0.0)).where(
                BudgetLedger.bucket == bucket
            )
        )
    return float(total or 0.0)


async def charge(
    sf: async_sessionmaker[AsyncSession],
    *,
    bucket: str,
    amount: float,
    ref: str,
    cap: float,
) -> bool:
    """Record a charge (idempotent on ``ref``). Returns True if still under cap."""
    async with sf() as session:
        session.add(BudgetLedger(bucket=bucket, amount=amount, ref=ref))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # already charged for this ref
    return (await spent(sf, bucket)) <= cap


async def over_budget(sf: async_sessionmaker[AsyncSession], *, bucket: str, cap: float) -> bool:
    return (await spent(sf, bucket)) > cap
