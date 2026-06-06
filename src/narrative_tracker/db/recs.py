"""Recommendation / gate-evaluation / outcome persistence (M6).

Closes the audit + feedback loop: every gated decision is recorded, broadcast
calls become ``live``, the scorer closes them into ``outcomes``, and
``closed_calls_for_credibility`` exposes them in the shape the credibility
recompute consumes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..schemas.call import TradeCall
from .models import GateEvaluation, Outcome, Recommendation


def _epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _save_gates(session, *, symbol: str, rec_id: int | None, gates: list[dict]) -> None:
    for g in gates:
        session.add(
            GateEvaluation(
                recommendation_id=rec_id, symbol=symbol,
                gate_name=g["name"], passed=g["passed"], measured=g.get("measured", {}),
            )
        )


async def save_recommendation(
    sf: async_sessionmaker[AsyncSession],
    *,
    call: TradeCall,
    credibility_at_issuance: float,
    sources: list[dict],
    gates: list[dict],
    issued_at: datetime,
    state: str = "candidate",
) -> int | None:
    """Persist a passed call + its gate evaluations. Idempotent on call_id."""
    async with sf() as session:
        rec = Recommendation(
            call_id=call.call_id, symbol=call.symbol, asset_class=call.asset_class,
            direction=call.direction.value, entry=call.entry, stop=call.stop,
            targets=call.targets.model_dump(), size_hint=call.size_hint, horizon=call.horizon,
            confidence=call.confidence, narrative=call.narrative, state=state,
            credibility_at_issuance=credibility_at_issuance, sources=sources, issued_at=issued_at,
        )
        session.add(rec)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return await session.scalar(
                select(Recommendation.id).where(Recommendation.call_id == call.call_id)
            )
        await _save_gates(session, symbol=call.symbol, rec_id=rec.id, gates=gates)
        await session.commit()
        return rec.id


async def save_suppressed(
    sf: async_sessionmaker[AsyncSession], *, symbol: str, gates: list[dict]
) -> None:
    """Record gate evaluations for a suppressed candidate (no rec row)."""
    async with sf() as session:
        await _save_gates(session, symbol=symbol, rec_id=None, gates=gates)
        await session.commit()


async def mark_live(sf: async_sessionmaker[AsyncSession], *, call_id: str) -> None:
    async with sf() as session:
        await session.execute(
            update(Recommendation).where(Recommendation.call_id == call_id).values(state="live")
        )
        await session.commit()


async def live_symbols(sf: async_sessionmaker[AsyncSession]) -> set[str]:
    async with sf() as session:
        rows = await session.scalars(
            select(Recommendation.symbol).where(Recommendation.state == "live")
        )
    return set(rows)


async def due_for_scoring(
    sf: async_sessionmaker[AsyncSession], *, now: datetime, max_age_s: int
) -> list[Recommendation]:
    """Live calls older than ``max_age_s`` (a simplified horizon for scoring)."""
    async with sf() as session:
        rows = await session.scalars(select(Recommendation).where(Recommendation.state == "live"))
        recs = list(rows)
    return [r for r in recs if (now - r.issued_at.replace(tzinfo=r.issued_at.tzinfo or timezone.utc)).total_seconds() >= max_age_s]


async def close_recommendation(
    sf: async_sessionmaker[AsyncSession],
    *,
    rec_id: int,
    close_reason: str,
    realized_r: float,
    mfe_r: float = 0.0,
    mae_r: float = 0.0,
    benchmark_r: float | None = None,
    closed_at: datetime,
) -> None:
    """Write the outcome + flip the rec to closed. Idempotent on rec_id."""
    async with sf() as session:
        session.add(
            Outcome(
                recommendation_id=rec_id, close_reason=close_reason, realized_r=realized_r,
                mfe_r=mfe_r, mae_r=mae_r, benchmark_r=benchmark_r, closed_at=closed_at,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return
        await session.execute(
            update(Recommendation).where(Recommendation.id == rec_id).values(state="closed")
        )
        await session.commit()


async def closed_calls_for_credibility(sf: async_sessionmaker[AsyncSession]) -> list[dict]:
    """All closed calls in the shape ``recompute_credibility`` consumes."""
    async with sf() as session:
        rows = (
            await session.execute(
                select(Recommendation, Outcome).join(
                    Outcome, Outcome.recommendation_id == Recommendation.id
                )
            )
        ).all()
    calls = []
    for rec, out in rows:
        calls.append(
            {
                "closed_at": _epoch(out.closed_at),
                "open_time": _epoch(rec.issued_at),
                "R": out.realized_r,
                "bench_R": out.benchmark_r or 0.0,
                "dir": 1 if rec.direction == "long" else -1,
                "contribs": rec.sources or [],
            }
        )
    return calls
