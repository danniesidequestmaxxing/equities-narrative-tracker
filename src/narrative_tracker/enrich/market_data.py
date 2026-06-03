"""Market-data snapshots (M3).

A provider returns a point-in-time :class:`MarketSnapshot` per instrument. The
real provider (Polygon/ORATS/CoinGecko) is injected; ``FakeMarketData`` is used in
tests. Snapshots feed the tradeability / staleness / numeric-sanity gates and the
call builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class MarketSnapshot:
    symbol: str
    asset_class: str
    price: float
    adv_usd: float            # 20-day average dollar volume (liquidity)
    spread_pct: float         # bid/ask spread %
    market_cap: float
    atr: float                # for stop sizing + numeric-sanity bounds
    as_of: datetime
    halted: bool = False
    optionable: bool = True
    iv_rank: float | None = None
    session_open: bool = True
    catalyst_within_h: float | None = None  # hours to next earnings/FDA/etc.

    def age_s(self, now: datetime) -> float:
        return (now - self.as_of).total_seconds()


class MarketDataProvider(Protocol):
    async def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot | None: ...


class FakeMarketData:
    def __init__(self, mapping: dict[str, MarketSnapshot]) -> None:
        self._m = mapping

    async def snapshot(self, symbol: str, asset_class: str) -> MarketSnapshot | None:
        return self._m.get(symbol)
