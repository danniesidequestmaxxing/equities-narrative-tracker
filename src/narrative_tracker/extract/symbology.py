"""Symbology: alias resolution + cross-asset classification (M1).

A small **seed** alias table maps company / product / person surface forms to a
ticker (production loads SEC EDGAR + Nasdaq/NYSE + Wikidata — this is the
pluggable point). ``classify_symbol`` applies the cross-asset policy from the
design ($ETH = crypto by default, $MSTR = equity, crypto-lexicon disambiguation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AliasEntry:
    symbol: str
    asset_class: str
    indirect: bool = False  # product/person reference -> cap confidence
    source: str = "seed"


# Seed surface -> AliasEntry. Multi-word keys are matched before their substrings.
ALIASES: dict[str, AliasEntry] = {
    "nvidia": AliasEntry("NVDA", "equity"),
    "apple": AliasEntry("AAPL", "equity"),
    "tesla": AliasEntry("TSLA", "equity"),
    "microsoft": AliasEntry("MSFT", "equity"),
    "amazon": AliasEntry("AMZN", "equity"),
    "meta": AliasEntry("META", "equity"),
    "alphabet": AliasEntry("GOOGL", "equity"),
    "broadcom": AliasEntry("AVGO", "equity"),
    "palantir": AliasEntry("PLTR", "equity"),
    "bitcoin": AliasEntry("BTC", "crypto"),
    "ethereum": AliasEntry("ETH", "crypto"),
    "solana": AliasEntry("SOL", "crypto"),
    # product / person -> issuer (indirect: lower confidence, provenance noted)
    "ozempic maker": AliasEntry("NVO", "equity", indirect=True, source="wikidata:manufacturer"),
    "ozempic": AliasEntry("NVO", "equity", indirect=True, source="wikidata:manufacturer"),
    "wegovy": AliasEntry("NVO", "equity", indirect=True, source="wikidata:manufacturer"),
    "zuck's company": AliasEntry("META", "equity", indirect=True, source="wikidata:employer"),
    "zuck": AliasEntry("META", "equity", indirect=True, source="wikidata:employer"),
}

# Bare cashtags that are canonically crypto.
CRYPTO_SYMBOLS: frozenset[str] = frozenset(
    {
        "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
        "BNB", "LTC", "ATOM", "APT", "ARB", "OP", "SUI", "SEI", "TIA", "INJ",
        "NEAR", "PEPE", "SHIB", "WIF", "BONK",
    }
)

# Crypto-adjacent EQUITIES that must never be classified as crypto.
ALWAYS_EQUITY: frozenset[str] = frozenset({"MSTR", "COIN", "HOOD", "RIOT", "MARA", "CLSK"})

_CRYPTO_LEXICON = (
    "chain", "wallet", "staking", "stake", "gas fee", "defi", "dex",
    "on-chain", "onchain", "airdrop", "mainnet", "blockchain", "token unlock",
)
_ETF_CUE = ("etf", "spot etf", "fund")


def classify_symbol(symbol: str, text: str) -> str:
    """Return the asset class for a bare cashtag given its post context."""
    s = symbol.upper().lstrip("$")
    low = (text or "").lower()
    if s in ALWAYS_EQUITY:
        return "equity"
    if s in CRYPTO_SYMBOLS:
        # An explicit ETF cue (e.g. "$ETH spot ETF") flips to ETF.
        if any(cue in low for cue in _ETF_CUE):
            return "etf"
        return "crypto"
    # Ambiguous symbol: crypto lexicon tips it toward crypto.
    if any(term in low for term in _CRYPTO_LEXICON):
        return "crypto"
    return "equity"


def find_aliases(text: str) -> list[tuple[str, AliasEntry]]:
    """Find cashtag-less company/product/person references in ``text``.

    Longest surface forms match first (so "ozempic maker" wins over "ozempic"),
    and overlapping spans are not double-counted.
    """
    low = (text or "").lower()
    used: list[tuple[int, int]] = []
    hits: list[tuple[str, AliasEntry]] = []
    for surface in sorted(ALIASES, key=len, reverse=True):
        pattern = rf"(?<![a-z]){re.escape(surface)}(?![a-z])"
        for m in re.finditer(pattern, low):
            span = m.span()
            if any(not (span[1] <= s or span[0] >= e) for s, e in used):
                continue
            used.append(span)
            hits.append((text[span[0] : span[1]], ALIASES[surface]))
    return hits
