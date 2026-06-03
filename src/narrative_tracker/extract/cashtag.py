"""Cashtag extraction (M0).

Handles the cheap, high-precision path: ``$AAPL`` / ``$BTC`` style cashtags.

Deliberately scoped to M0:
- The ``$``-prefixed regex requires a *letter*, so dollar amounts like ``$4200``
  or ``$3.50`` are not matched.
- ``.class`` suffixes are preserved (``$BRK.B`` -> ``BRK.B``).
- A small crypto set gives a best-effort asset class. Full cross-asset
  disambiguation and the common-word collision gate ($ALL, $ON, $IT, ...) are
  M1 — see docs/design/04-extraction-cascade.md.
"""

from __future__ import annotations

import re

# $ + 1-6 letters, optional .CLASS, word-bounded, not preceded by an alnum char
# (so emails / URLs / mid-word $ don't false-match).
CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z]{1,6})(\.[A-Za-z])?\b")

# Best-effort crypto tagging for M0. Some of these ($LINK, $UNI, $COMP) also
# collide with equity tickers — that disambiguation is M1.
CRYPTO_CASHTAGS: frozenset[str] = frozenset(
    {
        "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        "MATIC", "BNB", "LTC", "UNI", "ATOM", "APT", "ARB", "OP", "SUI",
        "SEI", "TIA", "INJ", "RNDR", "NEAR", "PEPE", "SHIB", "WIF", "BONK",
    }
)


def extract_cashtags(text: str) -> list[dict]:
    """Return a de-duplicated list of cashtag mentions found in ``text``.

    Each mention is a dict compatible with ``db.repo.add_mentions``:
    ``{symbol, asset_class, resolution_method, mention_confidence, surface}``.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for match in CASHTAG_RE.finditer(text or ""):
        root = match.group(1).upper()
        cls = (match.group(2) or "").upper()
        symbol = f"{root}{cls}"
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append(
            {
                "symbol": symbol,
                "asset_class": "crypto" if root in CRYPTO_CASHTAGS else "equity",
                "resolution_method": "cashtag_exact",
                "mention_confidence": 1.0,
                "surface": match.group(0),
            }
        )
    return out
