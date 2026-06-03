"""Narrative clustering + momentum (M2).

M2 uses **seed themes** (a curated controlled vocabulary) for deterministic,
testable assignment; production swaps in embedding-based anchor assignment behind
the same surface (docs/design/01-narrative-clustering.md). Momentum is a
dual-EWMA velocity model with a robust z-score noise floor.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field

# Seed themes: label -> {keywords, tickers}. Production discovers these nightly.
SEED_THEMES: dict[str, dict[str, frozenset[str]]] = {
    "AI infrastructure": {
        "keywords": frozenset({"ai", "gpu", "data center", "hyperscaler", "inference", "accelerator", "compute"}),
        "tickers": frozenset({"NVDA", "AMD", "AVGO", "SMCI", "VRT", "ARM", "MU", "TSM", "PLTR", "MRVL"}),
    },
    "GLP-1 / obesity": {
        "keywords": frozenset({"glp-1", "glp1", "ozempic", "wegovy", "obesity", "weight loss"}),
        "tickers": frozenset({"NVO", "LLY", "VKTX", "AMGN"}),
    },
    "Crypto / digital assets": {
        "keywords": frozenset({"bitcoin", "crypto", "blockchain", "ethereum", "defi", "stablecoin", "on-chain"}),
        "tickers": frozenset({"BTC", "ETH", "SOL", "COIN", "MSTR", "MARA", "RIOT", "HOOD"}),
    },
    "Nuclear / uranium": {
        "keywords": frozenset({"nuclear", "uranium", "smr", "reactor"}),
        "tickers": frozenset({"CCJ", "OKLO", "SMR", "LEU", "UEC"}),
    },
    "Quantum computing": {
        "keywords": frozenset({"quantum"}),
        "tickers": frozenset({"IONQ", "RGTI", "QBTS"}),
    },
}


def assign_narratives(symbol: str, text: str = "") -> list[str]:
    """Return the narrative labels a (symbol, text) belongs to."""
    sym = symbol.upper().lstrip("$")
    low = (text or "").lower()
    labels: list[str] = []
    for label, theme in SEED_THEMES.items():
        if sym in theme["tickers"] or any(kw in low for kw in theme["keywords"]):
            labels.append(label)
    return labels


@dataclass
class _NarrState:
    fast: float = 0.0
    slow: float = 0.0
    last_ts: float = 0.0
    prev_nu: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=300))


class NarrativeTracker:
    """Per-narrative credibility-weighted momentum."""

    def __init__(self, *, fast_s: int = 6 * 3600, slow_s: int = 24 * 3600) -> None:
        self._lam_f = math.log(2) / fast_s
        self._lam_s = math.log(2) / slow_s
        self._state: dict[str, _NarrState] = {}

    # Tunables (conservative defaults from design 01).
    NU_HI = 0.20
    NU_LO = 0.05
    Z_MIN = 2.0

    def add(self, label: str, *, weight: float, ts: float) -> None:
        st = self._state.setdefault(label, _NarrState(last_ts=ts))
        st.fast = st.fast * math.exp(-self._lam_f * max(0.0, ts - st.last_ts)) + weight
        st.slow = st.slow * math.exp(-self._lam_s * max(0.0, ts - st.last_ts)) + weight
        st.last_ts = ts

    def _rates(self, st: _NarrState, now: float) -> tuple[float, float]:
        dt = max(0.0, now - st.last_ts)
        fast = st.fast * math.exp(-self._lam_f * dt)
        slow = st.slow * math.exp(-self._lam_s * dt)
        return fast * self._lam_f, slow * self._lam_s  # convert decaying sums -> rates

    def _zscore(self, st: _NarrState, rate_fast: float) -> float:
        if len(st.history) < 5:
            return self.Z_MIN + 1  # a brand-new narrative's burst is significant
        med = statistics.median(st.history)
        mad = statistics.median([abs(x - med) for x in st.history]) or 1e-9
        return 0.6745 * (rate_fast - med) / mad

    def momentum(self, label: str, now: float) -> str:
        st = self._state.get(label)
        if st is None:
            return "dormant"
        rate_fast, rate_slow = self._rates(st, now)
        nu = (rate_fast - rate_slow) / (rate_slow + 1e-9)
        accel = nu - st.prev_nu
        st.prev_nu = nu

        if rate_fast < 1e-6:  # negligible recent activity
            st.history.append(rate_fast)
            return "dormant"

        z = self._zscore(st, rate_fast)
        st.history.append(rate_fast)

        if nu >= self.NU_HI and accel >= 0 and z >= self.Z_MIN:
            return "rising"
        if nu >= self.NU_HI and accel < 0:
            return "peaking"
        if nu <= self.NU_LO:
            return "fading"
        return "rising" if accel >= 0 else "fading"

    def labels(self) -> list[str]:
        return list(self._state)
