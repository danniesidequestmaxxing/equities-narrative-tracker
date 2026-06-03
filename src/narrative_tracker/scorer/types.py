"""Scorer value types (Decimal price math to keep stop/target tie-breaks exact)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

D = Decimal


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class CloseReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    EXPIRY = "expiry"
    INVALIDATED = "invalidated"
    TERMINAL = "terminal"  # delist / M&A — never bucketed as stop


class AdjKind(str, Enum):
    SPLIT = "split"        # value = ratio (2 => 2-for-1)
    DIVIDEND = "dividend"  # value = cash/share


@dataclass(frozen=True)
class Bar:
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = D(0)


@dataclass(frozen=True)
class Adjustment:
    ts: int
    kind: AdjKind
    value: Decimal


@dataclass(frozen=True)
class TerminalEvent:
    ts: int
    terminal_price: Decimal
    note: str = ""


@dataclass(frozen=True)
class Call:
    call_id: str
    symbol: str
    direction: Direction
    t0: int
    entry: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...]
    horizon_s: int
    benchmark: str = "SPY"
    invalidation: Optional[Callable[["Bar", dict], bool]] = None

    def __post_init__(self):
        if self.entry == self.stop:
            raise ValueError(f"{self.call_id}: entry == stop -> R undefined")
        if self.direction is Direction.LONG and self.stop >= self.entry:
            raise ValueError(f"{self.call_id}: long stop must be below entry")
        if self.direction is Direction.SHORT and self.stop <= self.entry:
            raise ValueError(f"{self.call_id}: short stop must be above entry")


@dataclass
class Outcome:
    call_id: str
    realized_r: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    reason: CloseReason
    entry_fill: Decimal
    close_px: Decimal
    close_ts: int
    bench_r: Optional[Decimal]
    rel_r: Optional[Decimal]
    entry_slippage_r: Decimal
    adjustments_applied: tuple[Adjustment, ...] = field(default_factory=tuple)
    status: str = "scored"
