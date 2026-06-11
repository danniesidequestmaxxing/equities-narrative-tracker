"""Polygon ("Massive") market-data provider (M7).

Conforms to ``MarketDataProvider``. HTTP is injected (``fetch``) so the parsing is
testable against recorded Polygon JSON; ``build_polygon_fetch`` wires real httpx
(lazy, part of the ``prod`` extra). Bars are fetched **unadjusted** (adjusted=false)
for the scorer; corporate actions come from the splits/dividends endpoints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from ..analyze.technicals import atr as atr_calc
from .market_data import MarketSnapshot

log = logging.getLogger(__name__)

Fetch = Callable[[str, dict], Awaitable[dict]]


def _date_to_epoch(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _equity_session_open(dt: datetime) -> bool:
    # Approx US RTH: Mon-Fri 13:30-20:00 UTC. Ignores DST/holidays;
    # pandas-market-calendars is the production upgrade.
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return 13 * 60 + 30 <= minutes <= 20 * 60


class PolygonMarketData:
    def __init__(self, *, fetch: Fetch, now: Callable[[], datetime] | None = None) -> None:
        self._fetch = fetch
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot | None:
        now = self._now()
        to_d, from_d = now.date(), now.date() - timedelta(days=60)
        aggs = await self._fetch(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{from_d}/{to_d}",
            {"adjusted": "true", "limit": 120},
        )
        results = aggs.get("results") or []
        if not results:
            return None
        closes = [r["c"] for r in results]
        highs = [r["h"] for r in results]
        lows = [r["l"] for r in results]
        vols = [r.get("v", 0.0) for r in results]
        price = closes[-1]
        recent = list(zip(closes, vols))[-20:]
        adv_usd = sum(c * v for c, v in recent) / len(recent) if recent else 0.0
        atr_val = atr_calc(highs, lows, closes, 14) or price * 0.02

        details = await self._fetch(f"/v3/reference/tickers/{symbol}", {})
        mcap = (details.get("results") or {}).get("market_cap", 0.0) or 0.0

        return MarketSnapshot(
            symbol=symbol, asset_class=asset_class, price=price, adv_usd=adv_usd,
            spread_pct=0.05, market_cap=mcap, atr=atr_val, as_of=now,
            session_open=(True if asset_class == "crypto" else _equity_session_open(now)),
        )

    async def fetch_bars(self, symbol: str, *, days: int = 400, adjusted: bool = False) -> list[dict]:
        # Unadjusted by default (the scorer's contract); the pulse TA passes
        # adjusted=True so indicators aren't distorted by splits.
        now = self._now()
        to_d, from_d = now.date(), now.date() - timedelta(days=days)
        data = await self._fetch(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{from_d}/{to_d}",
            {"adjusted": "true" if adjusted else "false", "limit": 50000},
        )
        return [
            {"ts": int(r["t"] // 1000), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v", 0.0)}
            for r in (data.get("results") or [])
        ]

    async def fetch_overview(self, symbol: str) -> dict:
        """Fundamentals-at-a-glance from the ticker reference endpoint."""
        data = await self._fetch(f"/v3/reference/tickers/{symbol}", {})
        r = data.get("results") or {}
        return {
            "name": r.get("name") or symbol,
            "market_cap": r.get("market_cap") or 0.0,
            "sector": r.get("sic_description") or "",
            "exchange": r.get("primary_exchange") or "",
        }

    async def fetch_adjustments(self, symbol: str) -> list[dict]:
        out: list[dict] = []
        splits = await self._fetch("/v3/reference/splits", {"ticker": symbol, "limit": 1000})
        for s in splits.get("results") or []:
            sfrom, sto, d = s.get("split_from"), s.get("split_to"), s.get("execution_date")
            if sfrom and sto and d:
                out.append({"ex_ts": _date_to_epoch(d), "kind": "split", "value": sto / sfrom})
        divs = await self._fetch("/v3/reference/dividends", {"ticker": symbol, "limit": 1000})
        for dv in divs.get("results") or []:
            d, cash = dv.get("ex_dividend_date"), dv.get("cash_amount")
            if d and cash:
                out.append({"ex_ts": _date_to_epoch(d), "kind": "dividend", "value": cash})
        return out


def build_polygon_fetch(api_key: str, *, base_url: str = "https://api.polygon.io") -> Fetch:  # pragma: no cover
    async def fetch(path: str, params: dict) -> dict:
        import httpx  # lazy: part of the `prod` extra

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(base_url + path, params={**params, "apiKey": api_key})
            resp.raise_for_status()
            return resp.json()

    return fetch
