"""Pure-Python technical indicators (M3).

No pandas dependency in the core (production can swap in pandas-ta-classic). Just
enough to characterize a setup: SMA, RSI, ATR, trend, and swing support/resistance.
"""

from __future__ import annotations


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / n, losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return round(sum(trs) / n, 4)


def trend(closes: list[float], fast: int = 20, slow: int = 50) -> str:
    f, s = sma(closes, fast), sma(closes, slow)
    if f is None or s is None:
        return "flat"
    if f > s * 1.001:
        return "up"
    if f < s * 0.999:
        return "down"
    return "flat"


def support_resistance(highs: list[float], lows: list[float], lookback: int = 20) -> tuple[float | None, float | None]:
    if not highs or not lows:
        return None, None
    window_h = highs[-lookback:]
    window_l = lows[-lookback:]
    return (min(window_l), max(window_h))


def snapshot_from_bars(bars: list[dict]) -> dict | None:
    """One-glance TA snapshot from ascending daily OHLCV bars (``o/h/l/c/v`` keys).

    Returns None when there's too little history to say anything (<30 bars).
    """
    if len(bars) < 30:
        return None
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [float(b.get("v") or 0.0) for b in bars]
    price = closes[-1]

    def chg(n: int) -> float | None:
        if len(closes) <= n or not closes[-1 - n]:
            return None
        return round((price / closes[-1 - n] - 1) * 100, 1)

    vol_base = sma(vols[:-1], 20)
    hi_52w = max(highs[-252:])
    lo_52w = min(lows[-252:])
    sup, res = support_resistance(highs, lows, 20)
    return {
        "price": price,
        "chg_1d": chg(1),
        "chg_5d": chg(5),
        "chg_20d": chg(20),
        "rsi": rsi(closes),
        "trend": trend(closes),
        "vol_ratio": round(vols[-1] / vol_base, 1) if vol_base else None,
        "off_high_pct": round((1 - price / hi_52w) * 100, 1) if hi_52w else None,
        "off_low_pct": round((price / lo_52w - 1) * 100, 1) if lo_52w else None,
        "support": sup,
        "resistance": res,
    }
