"""Path-dependent outcome scorer.

Explicit per-trade bar loop (readability > vectorization; volume is tiny). Closes
the four silent-corruption traps: look-ahead, intrabar tie-break, gap fills, and
corporate actions (replayed forward over as-of-issuance unadjusted bars).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional, Sequence

from .types import (
    Adjustment,
    AdjKind,
    Bar,
    Call,
    CloseReason,
    D,
    Direction,
    Outcome,
    TerminalEvent,
)


class _Plan:
    __slots__ = ("direction", "entry", "stop", "target", "risk")

    def __init__(self, call: Call):
        self.direction = call.direction
        self.entry = call.entry
        self.stop = call.stop
        self.target = call.targets[0]
        self.risk = abs(call.entry - call.stop)

    def r(self, price: Decimal) -> Decimal:
        if self.direction is Direction.LONG:
            return (price - self.entry) / self.risk
        return (self.entry - price) / self.risk

    def apply_split(self, ratio: Decimal) -> None:
        self.entry /= ratio
        self.stop /= ratio
        self.target /= ratio
        self.risk /= ratio


def _bar_at_or_before(bars: Sequence[Bar], ts: int) -> Optional[int]:
    lo, hi, ans = 0, len(bars) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].ts <= ts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _dividend_credit(plan: _Plan, ledger, start_ts, end_ts, direction) -> Decimal:
    cash = D(0)
    for adj in ledger:
        if adj.kind is AdjKind.DIVIDEND and start_ts < adj.ts <= end_ts:
            cash += adj.value
    signed = cash if direction is Direction.LONG else -cash
    return signed / plan.risk


def _update_excursions(plan, bar, long, mfe, mae):
    fav = plan.r(bar.high) if long else plan.r(bar.low)
    adv = plan.r(bar.low) if long else plan.r(bar.high)
    return max(mfe, fav), min(mae, adv)


def _disambiguate(loader, symbol, ts, stop, target, long):
    if loader is None:
        return None
    sub = loader(symbol, ts)
    if not sub:
        return None
    for b in sub:
        s = (b.low <= stop) if long else (b.high >= stop)
        t = (b.high >= target) if long else (b.low <= target)
        if s:
            return "stop"
        if t:
            return "target"
    return None


def _benchmark_r(bench, entry_ts, exit_ts, risk, direction):
    ei = _bar_at_or_before(bench, entry_ts)
    xi = _bar_at_or_before(bench, exit_ts)
    if ei is None or xi is None:
        return None
    b_entry, b_exit = bench[ei].open, bench[xi].close
    if b_entry == 0:
        return None
    move = (b_exit - b_entry) if direction is Direction.LONG else (b_entry - b_exit)
    bench_frac = move / b_entry
    return bench_frac * b_entry / risk  # = (move / risk); auditable alpha-in-R form


def score_call(
    call: Call,
    bars: Sequence[Bar],
    bench_bars: Sequence[Bar] = (),
    ledger: Sequence[Adjustment] = (),
    terminal: Optional[TerminalEvent] = None,
    subbar_loader: Optional[Callable] = None,
) -> Outcome:
    long = call.direction is Direction.LONG
    sig_idx = _bar_at_or_before(bars, call.t0)
    if sig_idx is None or sig_idx + 1 >= len(bars):
        return Outcome(
            call.call_id, D(0), D(0), D(0), CloseReason.EXPIRY, D(0), D(0), call.t0,
            None, None, D(0), status="pending",
        )
    fill = bars[sig_idx + 1]
    plan = _Plan(call)
    entry_fill = fill.open
    entry_slip = plan.r(entry_fill)

    horizon_ts = call.t0 + call.horizon_s
    window = [b for b in bars[sig_idx + 1:] if b.ts <= horizon_ts] or [fill]
    mfe, mae = D("-Infinity"), D("Infinity")
    applied: list[Adjustment] = []
    splits = sorted((a for a in ledger if a.kind is AdjKind.SPLIT), key=lambda a: a.ts)
    sp = 0
    realized = close_px = close_ts = reason = None

    for bar in window:
        while sp < len(splits) and splits[sp].ts <= bar.ts:
            plan.apply_split(splits[sp].value)
            applied.append(splits[sp])
            sp += 1
        if terminal is not None and bar.ts == terminal.ts:
            close_px = terminal.terminal_price
            mfe, mae = _update_excursions(plan, bar, long, mfe, mae)
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            reason, close_ts = CloseReason.TERMINAL, bar.ts
            break
        mfe, mae = _update_excursions(plan, bar, long, mfe, mae)
        if (bar.open <= plan.stop) if long else (bar.open >= plan.stop):
            close_px, reason, close_ts = bar.open, CloseReason.STOP, bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break
        if (bar.open >= plan.target) if long else (bar.open <= plan.target):
            close_px, reason, close_ts = bar.open, CloseReason.TARGET, bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break
        hit_stop = (bar.low <= plan.stop) if long else (bar.high >= plan.stop)
        hit_tgt = (bar.high >= plan.target) if long else (bar.low <= plan.target)
        if hit_stop and hit_tgt:
            first = _disambiguate(subbar_loader, call.symbol, bar.ts, plan.stop, plan.target, long)
            close_px, reason = (plan.target, CloseReason.TARGET) if first == "target" else (plan.stop, CloseReason.STOP)
            close_ts = bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break
        if hit_stop:
            close_px, reason, close_ts = plan.stop, CloseReason.STOP, bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break
        if hit_tgt:
            close_px, reason, close_ts = plan.target, CloseReason.TARGET, bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break
        if call.invalidation and call.invalidation(bar, {"plan": plan}):
            close_px, reason, close_ts = bar.close, CloseReason.INVALIDATED, bar.ts
            realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, bar.ts, call.direction)
            break

    if realized is None:
        last = window[-1]
        close_px, reason, close_ts = last.close, CloseReason.EXPIRY, last.ts
        realized = plan.r(close_px) + _dividend_credit(plan, ledger, fill.ts, last.ts, call.direction)

    bench_r = _benchmark_r(bench_bars, fill.ts, close_ts, plan.risk, call.direction) if bench_bars else None
    rel_r = (realized - bench_r) if bench_r is not None else None
    return Outcome(
        call.call_id, realized, mfe, mae, reason, entry_fill, close_px, close_ts,
        bench_r, rel_r, entry_slip, tuple(applied), "scored",
    )
