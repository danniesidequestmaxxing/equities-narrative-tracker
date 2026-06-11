"""Read-side analytics for the web dashboard (M-dashboard).

Computes ticker sentiment + hot-ticker rankings from the **durable** data
(ticker_mentions ⋈ posts ⋈ accounts) — independent of the worker's in-memory
EWMA state, so any process (the dashboard) can serve it. Credibility-weighted,
consistent with the system's sentiment model.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..analyze.sentiment import credibility_prior
from .models import Account, Post, TickerMention

_GAMMA = 1.5   # credibility exponent
_K = 2.0       # shrinkage prior (pull thin coverage toward neutral)
_STANCE_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0, "unclear": 0}


def _credibility(tier: str | None) -> float:
    # Tier prior (HOT/WARM/COLD) — the zero-evidence fallback.
    return credibility_prior(tier or "COLD")


def _cred_for(row: dict, scores: dict[int, float] | None) -> float:
    """Live evidence-weighted score when one exists (M10), else the tier prior."""
    if scores:
        live = scores.get(row.get("account_id"))
        if live is not None:
            return live
    return _credibility(row.get("tier"))


async def _mention_rows(
    sf: async_sessionmaker[AsyncSession], *, since: datetime, symbol: str | None = None
) -> list[dict]:
    stmt = (
        select(
            TickerMention.symbol, TickerMention.asset_class, TickerMention.stance,
            TickerMention.stance_confidence, TickerMention.mention_confidence,
            Post.text, Post.posted_at, Post.platform_post_id, Post.account_id,
            Account.handle, Account.tier,
        )
        .join(Post, TickerMention.post_id == Post.id)
        .join(Account, Post.account_id == Account.id)
        .where(Post.posted_at >= since)
        .order_by(Post.posted_at.desc())
    )
    if symbol:
        stmt = stmt.where(TickerMention.symbol == symbol)
    async with sf() as session:
        result = await session.execute(stmt)
        return [dict(r._mapping) for r in result]


def _sentiment(rows: list[dict], scores: dict[int, float] | None = None) -> tuple[float, float]:
    """Credibility-weighted sentiment in (-1, 1) + effective sample size."""
    num = den = q = 0.0
    for r in rows:
        sign = _STANCE_SIGN.get(r["stance"], 0)
        cred = _cred_for(r, scores)
        w = (cred ** _GAMMA) * (r["stance_confidence"] or 0.5)
        num += w * sign
        den += w
        q += w * w
    s = num / (den + _K) if den else 0.0
    n_eff = (den * den / q) if q else 0.0
    return round(s, 3), round(n_eff, 2)


def _tweet_url(handle: str, post_id: str) -> str:
    return f"https://x.com/{handle}/status/{post_id}" if handle and post_id else ""


async def ticker_detail(
    sf: async_sessionmaker[AsyncSession], *, symbol: str, since: datetime, limit: int = 50
) -> dict:
    from . import repo

    rows = await _mention_rows(sf, since=since, symbol=symbol)
    scores = await repo.latest_account_scores(sf)
    s, n_eff = _sentiment(rows, scores)
    takes = [
        {
            "handle": r["handle"],
            "tier": r["tier"],
            "credibility": round(_cred_for(r, scores), 2),
            "stance": r["stance"],
            "stance_confidence": round(r["stance_confidence"] or 0.0, 2),
            "asset_class": r["asset_class"],
            "text": r["text"],
            "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None,
            "url": _tweet_url(r["handle"], r["platform_post_id"]),
        }
        for r in rows[:limit]
    ]
    return {"symbol": symbol, "sentiment": s, "n_eff": n_eff, "mentions": len(rows), "takes": takes}


async def hot_tickers(
    sf: async_sessionmaker[AsyncSession], *, since: datetime, limit: int = 20
) -> list[dict]:
    from . import repo

    rows = await _mention_rows(sf, since=since)
    scores = await repo.latest_account_scores(sf)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    out = []
    for symbol, group in by_symbol.items():
        s, n_eff = _sentiment(group, scores)
        # heat = credibility-weighted activity (rewards accounts proven right)
        heat = sum(_cred_for(g, scores) for g in group)
        top = sorted(
            {g["handle"]: _cred_for(g, scores) for g in group}.items(),
            key=lambda kv: -kv[1],
        )[:3]
        out.append({
            "symbol": symbol,
            "asset_class": group[0]["asset_class"],
            "mentions": len(group),
            "accounts": len({g["handle"] for g in group}),
            "heat": round(heat, 2),
            "sentiment": s,
            "n_eff": n_eff,
            "top_accounts": [h for h, _ in top],
        })
    out.sort(key=lambda x: x["heat"], reverse=True)
    return out[:limit]
