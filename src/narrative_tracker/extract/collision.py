"""Common-word cashtag collision gate (M1).

Real US tickers that are also ordinary English words ($ALL, $ON, $IT, $AI,
$YOLO, ...). A naive cashtag regex over-fires on these. We gate them with a
local disambiguation rule (finance context, co-occurring cashtags, or the word
appearing lowercased elsewhere). Uncertain cases get a low confidence and are
left for the LLM stage / θ gate downstream.

This is a *starter* set — production regenerates it from a live exchange symbol
directory ∩ an English wordlist and re-curates (see docs/design/04).
"""

from __future__ import annotations

import re

# Real US-listed symbols that collide with common words/slang.
COLLISION_SET: frozenset[str] = frozenset(
    {
        "ALL", "ON", "IT", "SO", "ARE", "BE", "ANY", "NOW", "REAL", "OR",
        "AI", "DD", "GO", "BIG", "HAS", "ONE", "OUT", "LOVE", "PLAY", "CASH",
        "WORK", "LIFE", "FUN", "SEE", "RUN", "TRUE", "WELL", "GOOD", "PAY",
        "CAR", "TWO", "MOON", "YOLO", "FOR", "EOD", "ANY", "BY", "AT", "AN",
    }
)

FIN_LEXICON: tuple[str, ...] = (
    "call", "put", "earnings", "price target", " pt ", "shares", "long",
    "short", "eps", "guidance", "buy", "sell", "bullish", "bearish",
    "breakout", "support", "resistance", "%", "target", "strike", "option",
    "squeeze", "calls", "puts",
)

_CASHTAG_STRIP = re.compile(r"\$[A-Za-z]{1,6}(?:\.[A-Za-z])?")


def is_real_ticker(
    symbol: str, text: str, *, other_cashtags: int = 0
) -> tuple[bool | None, float]:
    """Decide whether a colliding cashtag is a real ticker.

    Returns ``(verdict, confidence)`` where verdict is ``True`` (real ticker),
    ``False`` (the English word — drop), or ``None`` (uncertain — keep with low
    confidence and let a later stage decide).
    """
    token = symbol.upper().lstrip("$")
    if token not in COLLISION_SET:
        return True, 0.97

    low = text.lower()
    has_fin = any(sig in low for sig in FIN_LEXICON)
    if has_fin or other_cashtags > 0:
        return True, 0.85

    # Does the word appear lowercased as a standalone word (not the cashtag)?
    without_cashtags = _CASHTAG_STRIP.sub(" ", low)
    used_as_word = (
        re.search(rf"(?<![a-z]){re.escape(token.lower())}(?![a-z])", without_cashtags)
        is not None
    )
    if used_as_word:
        return False, 0.6
    return None, 0.5
