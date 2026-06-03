---
title: "Design: Path-Dependent Outcome Scorer"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M4
modules: [scorer/types.py, scorer/core.py, scorer/calendar.py, db/adjustments.py, tests/test_golden_fixtures.py]
---

# Path-Dependent Outcome Scorer — Implementation Spec

The correctness core of the feedback loop. Given a *call* (direction/entry/stop/target/horizon) and market bars, compute realized outcome — R-multiple, MFE, MAE, close reason, benchmark-relative R — **without look-ahead, with explicit path-dependence, with corporate actions as an as-of-issuance ledger, survivorship-safe.**

The vectorized `(close > target).idxmax()` approach is wrong on four axes (entry timing, intrabar tie-break, gap fills, corporate actions) and corrupts results *silently*. The fix is an explicit per-trade bar loop. Volume is tiny (10²–10⁴ closed calls) → readability wins.

## 0. Definitions & sign conventions
```
risk_per_share = |entry − stop|              # > 0, fixed at issuance, never recomputed
R(price) = (price − entry)/risk_per_share    # LONG
         = (entry − price)/risk_per_share    # SHORT
```
Fill at stop ⇒ R=−1; at 1R target ⇒ R=+1. **MFE** = max R(favorable bar extreme: high for long, low for short); **MAE** = min R(adverse extreme). Reported raw (not clamped). **realized_R** = R at the actual fill price of the closing event + per-share dividend/borrow adjustments, /risk.

**Close reasons (exhaustive):** `target | stop | expiry | invalidated | terminal`. The fifth (`terminal`, delist/M&A) must not be bucketed as `stop` — that would bias stop stats. Alias `terminal→stop` only in reporting, never the raw record.

## 1. Algorithm (scoring one call)
```
SCORE(call, bars, bench_bars, ledger, terminal_events):
  # 1. Fill bar (NO LOOK-AHEAD)
  signal_bar = last bar with ts <= call.t0
  fill_bar   = first bar with ts  > signal_bar.ts        # the NEXT bar
  if none: return PENDING
  entry_fill = fill_bar.open      # entry at next-bar OPEN. Risk stays anchored to PLANNED entry/stop.

  # 2. Window = fill bar .. horizon
  window = [b for b in bars if fill_bar.ts <= b.ts <= horizon_ts]
  mfe=-inf; mae=+inf; realized=None

  for bar in window:
      apply_due_splits(bar.ts, ledger)          # rescale entry/stop/target ONCE at split bar
      if terminal_event(bar.ts): close at terminal_price; reason=terminal; break
      mfe,mae = update_excursions(bar)
      if gaps_through_stop(bar.open):  close=bar.open; reason=stop;   break   # gap fills at OPEN
      if gaps_through_target(bar.open):close=bar.open; reason=target; break
      if hit_stop and hit_target:                # intrabar both
          sub = load_subbars(bar.ts)
          close,reason = (target if first(sub)==target else stop) if sub else (stop, "stop")  # STOP-FIRST default
          break
      if hit_stop:   close=stop;   reason=stop;   break
      if hit_target: close=target; reason=target; break
      if call.invalidation(bar): close=bar.close; reason=invalidated; break

  if realized is None:                           # expiry mark-out
      close = window[-1].close; reason=expiry
  bench_R = benchmark_R_over(bench_bars, fill_bar.ts, close_ts, risk, direction)
  return Outcome(realized, mfe, mae, reason, bench_R, rel_R=realized−bench_R, ...)
```
**Stop-first** when a daily bar straddles both and you have no intrabar data: you genuinely don't know order → conservative estimate biases toward *underclaiming* edge (safe for capital allocation). Escape hatch = real sub-bar data, not optimism.

## 2. Reference implementation

### scorer/types.py
```python
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional, Sequence
D = Decimal

class Direction(str, Enum): LONG="long"; SHORT="short"
class CloseReason(str, Enum): TARGET="target"; STOP="stop"; EXPIRY="expiry"; INVALIDATED="invalidated"; TERMINAL="terminal"
class AdjKind(str, Enum): SPLIT="split"; DIVIDEND="dividend"

@dataclass(frozen=True)
class Bar:
    ts: int; open: Decimal; high: Decimal; low: Decimal; close: Decimal
    volume: Decimal = D(0); asof_unadjusted: bool = True

@dataclass(frozen=True)
class Adjustment:
    ts: int; kind: AdjKind; value: Decimal   # SPLIT: ratio (2=2-for-1). DIVIDEND: cash/share.

@dataclass(frozen=True)
class TerminalEvent:
    ts: int; terminal_price: Decimal; note: str = ""

@dataclass(frozen=True)
class Call:
    call_id: str; symbol: str; direction: Direction; t0: int
    entry: Decimal; stop: Decimal; targets: tuple[Decimal, ...]; horizon_s: int
    benchmark: str = "SPY"; invalidation: Optional[Callable[["Bar", dict], bool]] = None
    def __post_init__(self):
        if self.entry == self.stop: raise ValueError(f"{self.call_id}: entry==stop -> R undefined")
        if self.direction is Direction.LONG and self.stop >= self.entry: raise ValueError("long stop must be < entry")
        if self.direction is Direction.SHORT and self.stop <= self.entry: raise ValueError("short stop must be > entry")

@dataclass
class Outcome:
    call_id: str; realized_R: Decimal; mfe_R: Decimal; mae_R: Decimal; reason: CloseReason
    entry_fill: Decimal; close_px: Decimal; close_ts: int; bench_R: Optional[Decimal]
    rel_R: Optional[Decimal]; entry_slippage_R: Decimal
    adjustments_applied: tuple[Adjustment, ...] = field(default_factory=tuple); status: str = "scored"
```

### scorer/core.py
```python
from __future__ import annotations
from decimal import Decimal
from typing import Callable, Optional, Sequence
from .types import Bar, Call, Adjustment, AdjKind, TerminalEvent, Direction, CloseReason, Outcome, D

class _Plan:
    __slots__ = ("direction","entry","stop","target","risk")
    def __init__(self, call: Call):
        self.direction=call.direction; self.entry=call.entry; self.stop=call.stop
        self.target=call.targets[0]; self.risk=abs(call.entry-call.stop)
    def r(self, price: Decimal) -> Decimal:
        return (price-self.entry)/self.risk if self.direction is Direction.LONG else (self.entry-price)/self.risk
    def apply_split(self, ratio: Decimal) -> None:        # 2-for-1: prices & risk halve -> R invariant
        self.entry/=ratio; self.stop/=ratio; self.target/=ratio; self.risk/=ratio

def _bar_at_or_before(bars, ts):
    lo, hi, ans = 0, len(bars)-1, None
    while lo <= hi:
        mid=(lo+hi)//2
        if bars[mid].ts <= ts: ans=mid; lo=mid+1
        else: hi=mid-1
    return ans

def _dividend_credit(plan, ledger, start_ts, end_ts, direction):
    cash = D(0)
    for adj in ledger:
        if adj.kind is AdjKind.DIVIDEND and start_ts < adj.ts <= end_ts: cash += adj.value
    signed = cash if direction is Direction.LONG else -cash
    return signed/plan.risk

def _update_excursions(plan, bar, long, mfe, mae):
    fav = plan.r(bar.high) if long else plan.r(bar.low)
    adv = plan.r(bar.low)  if long else plan.r(bar.high)
    return max(mfe, fav), min(mae, adv)

def score_call(call, bars, bench_bars, ledger=(), terminal=None, subbar_loader=None):
    LONG = call.direction is Direction.LONG
    sig_idx = _bar_at_or_before(bars, call.t0)
    if sig_idx is None or sig_idx+1 >= len(bars):
        return Outcome(call.call_id, D(0),D(0),D(0), CloseReason.EXPIRY, D(0),D(0), call.t0, None,None,D(0), status="pending")
    fill = bars[sig_idx+1]; plan=_Plan(call); entry_fill=fill.open; entry_slip_R=plan.r(entry_fill)
    horizon_ts = call.t0 + call.horizon_s
    window = [b for b in bars[sig_idx+1:] if b.ts <= horizon_ts] or [fill]
    mfe, mae = D("-Infinity"), D("Infinity"); applied=[]
    splits = sorted((a for a in ledger if a.kind is AdjKind.SPLIT), key=lambda a:a.ts); sp=0
    realized=close_px=close_ts=reason=None
    for bar in window:
        while sp < len(splits) and splits[sp].ts <= bar.ts:
            plan.apply_split(splits[sp].value); applied.append(splits[sp]); sp+=1
        if terminal is not None and bar.ts == terminal.ts:
            close_px=terminal.terminal_price; mfe,mae=_update_excursions(plan,bar,LONG,mfe,mae)
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction)
            reason,close_ts=CloseReason.TERMINAL,bar.ts; break
        mfe,mae=_update_excursions(plan,bar,LONG,mfe,mae)
        if (bar.open<=plan.stop) if LONG else (bar.open>=plan.stop):
            close_px,reason,close_ts=bar.open,CloseReason.STOP,bar.ts
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
        if (bar.open>=plan.target) if LONG else (bar.open<=plan.target):
            close_px,reason,close_ts=bar.open,CloseReason.TARGET,bar.ts
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
        hit_stop=(bar.low<=plan.stop) if LONG else (bar.high>=plan.stop)
        hit_tgt =(bar.high>=plan.target) if LONG else (bar.low<=plan.target)
        if hit_stop and hit_tgt:
            first=_disambiguate(subbar_loader,call.symbol,bar.ts,plan.stop,plan.target,LONG)
            close_px,reason=(plan.target,CloseReason.TARGET) if first=="target" else (plan.stop,CloseReason.STOP)
            close_ts=bar.ts; realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
        if hit_stop:
            close_px,reason,close_ts=plan.stop,CloseReason.STOP,bar.ts
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
        if hit_tgt:
            close_px,reason,close_ts=plan.target,CloseReason.TARGET,bar.ts
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
        if call.invalidation and call.invalidation(bar, {"plan":plan}):
            close_px,reason,close_ts=bar.close,CloseReason.INVALIDATED,bar.ts
            realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,bar.ts,call.direction); break
    if realized is None:
        last=window[-1]; close_px,reason,close_ts=last.close,CloseReason.EXPIRY,last.ts
        realized=plan.r(close_px)+_dividend_credit(plan,ledger,fill.ts,last.ts,call.direction)
    bench_R=_benchmark_R(bench_bars,fill.ts,close_ts,plan.risk,call.direction)
    rel_R=(realized-bench_R) if bench_R is not None else None
    return Outcome(call.call_id,realized,mfe,mae,reason,entry_fill,close_px,close_ts,bench_R,rel_R,
                   entry_slip_R,tuple(applied),"scored")

def _disambiguate(loader, symbol, ts, stop, target, long):
    if loader is None: return None
    sub = loader(symbol, ts)
    if not sub: return None
    for b in sub:
        s=(b.low<=stop) if long else (b.high>=stop); t=(b.high>=target) if long else (b.low<=target)
        if s and t: return "stop"
        if s: return "stop"
        if t: return "target"
    return None

def _benchmark_R(bench, entry_ts, exit_ts, risk, direction):
    ei=_bar_at_or_before(bench,entry_ts); xi=_bar_at_or_before(bench,exit_ts)
    if ei is None or xi is None: return None
    b_entry,b_exit = bench[ei].open, bench[xi].close
    move = (b_exit-b_entry) if direction is Direction.LONG else (b_entry-b_exit)
    bench_frac = move/b_entry
    return bench_frac * b_entry / risk     # = (realized_frac - bench_frac)/(risk/entry) form, auditable
```
> **Benchmark normalization** is the one genuinely ambiguous choice. Recommended explicit form: `rel_R = (realized_frac − bench_frac) / (risk/entry)` — same quantity, reviewer-verifiable by eye. `entry_slippage_R` (planned entry vs `entry_fill`) is reported separately, never folded into `realized_R`.

## 3. Golden fixtures (exact expected R)
| # | Fixture | Setup (LONG unless noted) | reason | realized_R |
|---|---|---|---|---|
| F1 | gap-through-stop | entry100 stop95 tgt110; D2 opens **92** | stop | **−1.6** |
| F2 | same-bar stop+target, no sub-bars | entry100 stop95 tgt105; D2 H106 L94 | stop | **−1.0** |
| F2b | same-bar, sub-bars say target first | 1m shows 105@10:05, 95@14:00 | target | **+1.0** |
| F3 | 2-for-1 split mid-trade | entry100 stop90 tgt130; split D3; D5 unadj H66=130 pre | target | **+3.0** |
| F4 | dividend mid-trade | entry100 stop95 tgt110; $2 div D3; exit 110 | target | **+2.4** |
| F5 | expiry-at-mark | entry100 stop90 tgt120; never tags; D6 close104 | expiry | **+0.4** |
| F6 | delist-to-zero | entry100 stop95; halt+delist D4 px0 | terminal | **−20.0** |
| F7 | never-hit flat | entry100 stop90 tgt110; osc 99–102, close101 | expiry | **+0.1** |
| F8 | SHORT gap-through-target | short entry100 stop105 tgt90; D2 opens 86 | target | **+2.8** |
| F9 | no-look-ahead guard | signal D1; D1 high ≥ target → fill is D2 open | (D2) | — |
| F10 | M&A cash-out | entry100 stop90; acquired $118 D7 | terminal | **+1.8** |

F1 spelled out (the canonical trap — gap hits both entry fill and stop on D2):
```python
def test_F1_gap_through_stop():
    bars=[Bar(day(1),D("100"),D("101"),D("99"),D("100")),
          Bar(day(2),D("92"),D("93"),D("90"),D("91")),       # GAPS below 95
          Bar(day(3),D("91"),D("96"),D("90"),D("95"))]
    call=Call("F1","ACME",Direction.LONG,day(1),D("100"),D("95"),(D("110"),),10*DAY)
    out=score_call(call,bars,FLAT_BENCH)
    assert out.reason is CloseReason.STOP
    assert out.realized_R == D("-1.6")     # fill at OPEN 92, not stop 95
    assert out.entry_fill == D("92")
```
Plus a `hypothesis` property test: random monotone-up bars → a long ends `target`/`expiry`, never `stop`; for mark-outs `mae_R ≤ realized_R ≤ mfe_R`; `mae_R ≤ −1` when reason=`stop` without a gap.

## 4. As-of-issuance unadjusted bars + adjustment ledger

**Principle:** what you know at score time must equal what you knew at issuance + an explicit replayable corporate-action ledger. Re-pulling today's adjusted closes is back-door look-ahead. Vendor reality (Polygon/"Massive"): returns split-adjusted by default and **does not dividend-adjust**; pass `adjusted=false` for unadjusted bars + read splits from the corporate-actions endpoint.

```sql
CREATE TABLE market_bars (
    symbol TEXT NOT NULL, ts BIGINT NOT NULL, timespan TEXT NOT NULL,
    open NUMERIC(20,8) NOT NULL, high NUMERIC(20,8) NOT NULL, low NUMERIC(20,8) NOT NULL,
    close NUMERIC(20,8) NOT NULL, volume NUMERIC(28,8) NOT NULL DEFAULT 0,
    adjustment_basis TEXT NOT NULL DEFAULT 'unadjusted',   -- ALWAYS unadjusted
    source TEXT NOT NULL, fetched_at BIGINT NOT NULL, asof_factor_date BIGINT,
    vendor_payload_id TEXT, session TEXT NOT NULL DEFAULT 'rth', is_halted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (symbol, timespan, ts, adjustment_basis, source));

CREATE TABLE adjustments (
    symbol TEXT NOT NULL, ex_ts BIGINT NOT NULL, kind TEXT NOT NULL,   -- split|dividend|spinoff|delist|merger
    value NUMERIC(28,12) NOT NULL, currency TEXT DEFAULT 'USD', declared_ts BIGINT,
    source TEXT NOT NULL, ingested_at BIGINT NOT NULL, raw_ref TEXT,
    PRIMARY KEY (symbol, ex_ts, kind, source));
```
Bars are **append-only, never UPDATEd**; a vendor restatement writes a new row. `db/adjustments.py` exposes `ingest_bars_unadjusted`, `ingest_corporate_actions`, `load_bars`, `load_ledger` (splits+dividends in `(start,end]`), `load_terminal` (delist/merger → defined terminal P&L; survivorship-safe). The scorer reads `load_bars(unadjusted)` + `load_ledger()` and replays forward → structurally impossible to pick up a post-issuance adjustment factor.

## 5. Crypto vs equities (behind a MarketCalendar)
- **Horizon:** equities = trading-session count (`calendar.add_sessions`, skip weekends/holidays); crypto = wall-clock (`t0 + n*86400`).
- **Gaps:** equities gap overnight/weekend (step 3d is load-bearing — most stop violations are at the open auction); crypto mostly continuous but keep gap handling (outages, de-pegs, flash crashes).
- **Halts/terminal:** equities LULD halts non-tradeable, delist/M&A = `terminal`; crypto exchange-delisting/chain-failure = `terminal_price=0`. Pin a **reference venue** per crypto symbol (no silent failover = no venue look-ahead).
```python
class MarketCalendar:
    def add_sessions(self, t0, n): ...
    def is_open(self, ts): ...
    def next_session_open_ts(self, ts): ...
class CryptoCalendar(MarketCalendar): ...   # 24/7
class EquityCalendar(MarketCalendar): ...   # XNYS, holidays, half-days, LULD (pandas-market-calendars)
```

## Sources
- Polygon/Massive adjusted data: https://polygon.io/knowledge-base/article/is-polygons-stock-data-adjusted-for-splits-or-dividends · splits endpoint: https://massive.com/docs/rest/stocks/corporate-actions/splits
- EODHD raw OHLC + splits/dividends: https://eodhd.com/financial-apis/api-splits-dividends · Quodd adjusted/unadjusted + corp actions: https://www.quodd.com/historical-stock-prices-api-global-market-data
