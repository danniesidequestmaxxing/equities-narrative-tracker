"""Event-study math (M9): what happened after a mention.

Pure functions over ascending split-adjusted daily bars (``ts`` epoch seconds,
``c`` close). The anchor is the first close on/after the post's calendar date —
i.e. "the price you could realistically reference end-of-day". Forward returns
are close-to-close, ``h`` trading days after the anchor.

Honest-measurement notes baked into the design:
* adjusted closes, so splits don't fabricate 90% "moves";
* a row can be partially complete (fwd_5d needs five sessions to exist) and is
  re-completed on later runs;
* returns measure information value, not tradability — no slippage/fills here.
"""

from __future__ import annotations

from datetime import datetime, timezone

HORIZONS = (1, 3, 5)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def anchor_index(bars: list[dict], posted_at: datetime) -> int | None:
    """Index of the first bar whose calendar date is on/after the post date.
    None when the post is newer than every bar (no anchor close yet)."""
    post_date = _utc(posted_at).date()
    for i, b in enumerate(bars):
        bar_date = datetime.fromtimestamp(int(b["ts"]), tz=timezone.utc).date()
        if bar_date >= post_date:
            return i
    return None


def forward_returns(
    bars: list[dict], posted_at: datetime, horizons: tuple[int, ...] = HORIZONS
) -> dict | None:
    """``{"px_post": float, "fwd": {h: pct|None}}`` or None when no anchor exists.

    ``fwd[h]`` is the fractional return from the anchor close to the close ``h``
    trading days later (None until that bar exists)."""
    if not bars:
        return None
    i = anchor_index(bars, posted_at)
    if i is None:
        return None
    px = float(bars[i]["c"])
    if px <= 0:
        return None
    fwd: dict[int, float | None] = {}
    for h in horizons:
        j = i + h
        fwd[h] = round(float(bars[j]["c"]) / px - 1, 6) if j < len(bars) else None
    return {"px_post": px, "fwd": fwd}


def stated_call_outcome(
    bars: list[dict],
    *,
    stated_at: datetime,
    direction: str,
    entry: float | None,
    stop: float | None,
    targets: list[float],
    horizon_days: int = 10,
) -> dict | None:
    """Score a stated call against the daily path: which came first — their
    stop, their first target, or the timeout?

    Entry defaults to the anchor close when unstated ("long $X" with no price).
    ``realized_r`` (profit in units of stated risk) only exists when a sane stop
    was given; ``realized_pct`` (direction-signed move from entry) always does.
    Conservative tie-break: a bar that touches both stop and target counts as a
    stop. Returns None while the call is still open (not enough bars yet).
    """
    i = anchor_index(bars, stated_at)
    if i is None:
        return None
    sign = 1 if direction == "long" else -1
    e = float(entry) if entry else float(bars[i]["c"])
    if e <= 0:
        return None
    t1 = float(targets[0]) if targets else None
    risk = (e - float(stop)) * sign if stop is not None else None
    if risk is not None and risk <= 0:
        risk = None  # stop on the wrong side of entry -> score directional-only

    def _result(reason: str, exit_px: float, ts: int) -> dict:
        pct = (exit_px / e - 1) * sign
        return {
            "reason": reason,
            "realized_pct": round(pct, 6),
            "realized_r": round((exit_px - e) * sign / risk, 4) if risk else None,
            "closed_ts": int(ts),
        }

    last = min(i + horizon_days, len(bars) - 1)
    for j in range(i + 1, last + 1):
        b = bars[j]
        hit_stop = stop is not None and (
            float(b["l"]) <= float(stop) if sign > 0 else float(b["h"]) >= float(stop)
        )
        hit_target = t1 is not None and (
            float(b["h"]) >= t1 if sign > 0 else float(b["l"]) <= t1
        )
        if hit_stop:
            return _result("stop", float(stop), b["ts"])
        if hit_target:
            return _result("target", t1, b["ts"])
    if i + horizon_days < len(bars):
        b = bars[i + horizon_days]
        return _result("timeout", float(b["c"]), b["ts"])
    return None  # horizon not reached yet


def signed_excess(stance: str, fwd: float | None, bench: float | None) -> float | None:
    """The 'edge if you traded their direction' number: benchmark-adjusted
    return, sign-flipped for bearish takes. None for neutral/unclear stances
    (no direction to trade) or missing data."""
    if fwd is None:
        return None
    sign = 1 if stance == "bullish" else -1 if stance == "bearish" else 0
    if sign == 0:
        return None
    excess = fwd - bench if bench is not None else fwd
    return round(sign * excess, 6)
