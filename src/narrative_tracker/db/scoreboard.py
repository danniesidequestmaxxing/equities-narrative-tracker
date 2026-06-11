"""Account scoreboard (M9-B): who is actually right, by how much.

Aggregates mention_outcomes into per-account stats. The headline metric is the
direction-signed, benchmark-adjusted 3-day return — "your average edge per
mention if you had traded their direction". Small samples are quarantined, not
ranked: hit rates on two mentions are astrology, and the UI says so.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..analyze.outcomes import signed_excess
from . import outcomes as db_outcomes

MIN_RANKED_N = 3


def _aggregate(rows: list[dict], *, min_n: int = MIN_RANKED_N) -> dict:
    """Pure aggregation: outcome rows -> {"ranked": [...], "thin": [...]}."""
    by_handle: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_handle[r["handle"]].append(r)

    accounts = []
    for handle, group in by_handle.items():
        edges = {1: [], 3: [], 5: []}
        per_symbol_3d: list[tuple[str, float]] = []
        for r in group:
            for h in (1, 3, 5):
                e = signed_excess(r["stance"], r.get(f"fwd_{h}d"), r.get(f"bench_{h}d"))
                if e is not None:
                    edges[h].append(e)
                    if h == 3:
                        per_symbol_3d.append((r["symbol"], e))
        n3 = len(edges[3])
        avg = {h: (round(sum(v) / len(v), 4) if v else None) for h, v in edges.items()}
        best = max(per_symbol_3d, key=lambda t: t[1], default=None)
        worst = min(per_symbol_3d, key=lambda t: t[1], default=None)
        accounts.append({
            "handle": handle,
            "tier": group[0]["tier"],
            "outcomes": len(group),
            "n": n3,
            "hit_3d": round(sum(1 for e in edges[3] if e > 0) / n3, 3) if n3 else None,
            "avg_1d": avg[1], "avg_3d": avg[3], "avg_5d": avg[5],
            "best": {"symbol": best[0], "edge": round(best[1], 4)} if best else None,
            "worst": {"symbol": worst[0], "edge": round(worst[1], 4)} if worst else None,
        })

    ranked = sorted(
        (a for a in accounts if a["n"] >= min_n),
        key=lambda a: a["avg_3d"] if a["avg_3d"] is not None else -9e9,
        reverse=True,
    )
    thin = sorted((a for a in accounts if a["n"] < min_n), key=lambda a: -a["n"])
    return {"ranked": ranked, "thin": thin}


async def account_scoreboard(sf, *, since: datetime, min_n: int = MIN_RANKED_N) -> dict:
    rows = await db_outcomes.outcomes_for_accounts(sf, since=since)
    return _aggregate(rows, min_n=min_n)


async def account_detail(sf, *, handle: str, since: datetime, recent: int = 6) -> dict:
    rows = [
        r for r in await db_outcomes.outcomes_for_accounts(sf, since=since)
        if r["handle"].lower() == handle.lower().lstrip("@")
    ]
    agg = _aggregate(rows, min_n=1)
    stats = (agg["ranked"] or agg["thin"] or [None])[0]
    takes = [
        {
            "symbol": r["symbol"], "stance": r["stance"],
            "posted_at": r["posted_at"],
            "edge_3d": signed_excess(r["stance"], r.get("fwd_3d"), r.get("bench_3d")),
        }
        for r in rows[:recent]
    ]
    return {"handle": handle.lstrip("@"), "stats": stats, "recent": takes}
