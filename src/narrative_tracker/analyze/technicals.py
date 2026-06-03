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
