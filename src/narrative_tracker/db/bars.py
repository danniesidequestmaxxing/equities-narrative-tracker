"""Market-bar + adjustment persistence and the providers the scorer reads (M7).

Bars are stored **unadjusted** and immutable; splits/dividends live in a separate
ledger and are replayed forward by the scorer (no back-door look-ahead — INV-5).
"""

from __future__ import annotations

from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..scorer.types import Adjustment as ScoreAdj
from ..scorer.types import AdjKind, Bar
from .models import Adjustment, MarketBar


async def save_bars(
    sf: async_sessionmaker[AsyncSession],
    *,
    symbol: str,
    interval: str,
    source: str,
    bars: list[dict],
) -> int:
    """Insert bars idempotently (skip ts already stored). Returns count inserted."""
    if not bars:
        return 0
    async with sf() as session:
        existing = set(
            await session.scalars(
                select(MarketBar.ts).where(
                    MarketBar.symbol == symbol,
                    MarketBar.interval == interval,
                    MarketBar.source == source,
                )
            )
        )
        added = 0
        for b in bars:
            if b["ts"] in existing:
                continue
            session.add(
                MarketBar(
                    symbol=symbol, interval=interval, ts=b["ts"], open=b["o"], high=b["h"],
                    low=b["l"], close=b["c"], volume=b.get("v", 0.0), source=source,
                )
            )
            added += 1
        await session.commit()
        return added


async def save_adjustments(
    sf: async_sessionmaker[AsyncSession], *, symbol: str, source: str, adjustments: list[dict]
) -> int:
    if not adjustments:
        return 0
    async with sf() as session:
        rows = (
            await session.execute(
                select(Adjustment.ex_ts, Adjustment.kind).where(
                    Adjustment.symbol == symbol, Adjustment.source == source
                )
            )
        ).all()
        existing = {(r.ex_ts, r.kind) for r in rows}
        added = 0
        for a in adjustments:
            if (a["ex_ts"], a["kind"]) in existing:
                continue
            session.add(
                Adjustment(symbol=symbol, ex_ts=a["ex_ts"], kind=a["kind"], value=a["value"], source=source)
            )
            added += 1
        await session.commit()
        return added


async def load_bars(
    sf: async_sessionmaker[AsyncSession], *, symbol: str, interval: str = "1d", source: str = "polygon"
) -> list[Bar]:
    async with sf() as session:
        rows = await session.scalars(
            select(MarketBar)
            .where(MarketBar.symbol == symbol, MarketBar.interval == interval, MarketBar.source == source)
            .order_by(MarketBar.ts)
        )
        rows = list(rows)
    return [
        Bar(r.ts, D(str(r.open)), D(str(r.high)), D(str(r.low)), D(str(r.close)), D(str(r.volume)))
        for r in rows
    ]


async def load_ledger(
    sf: async_sessionmaker[AsyncSession], *, symbol: str, source: str = "polygon"
) -> list[ScoreAdj]:
    async with sf() as session:
        rows = await session.scalars(
            select(Adjustment).where(Adjustment.symbol == symbol, Adjustment.source == source).order_by(Adjustment.ex_ts)
        )
        rows = list(rows)
    return [ScoreAdj(r.ex_ts, AdjKind(r.kind), D(str(r.value))) for r in rows]


class DbBarsProvider:
    """Callable ``(symbol) -> list[Bar]`` for run_scoring."""

    def __init__(self, sf, *, interval: str = "1d", source: str = "polygon") -> None:
        self._sf, self._interval, self._source = sf, interval, source

    async def __call__(self, symbol: str) -> list[Bar]:
        return await load_bars(self._sf, symbol=symbol, interval=self._interval, source=self._source)


class DbLedgerProvider:
    """Callable ``(symbol) -> list[Adjustment]`` for run_scoring."""

    def __init__(self, sf, *, source: str = "polygon") -> None:
        self._sf, self._source = sf, source

    async def __call__(self, symbol: str) -> list[ScoreAdj]:
        return await load_ledger(self._sf, symbol=symbol, source=self._source)
