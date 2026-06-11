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
