"""Social-signal analytics (M2): sentiment, contrarian extremes, pump detection.

All deterministic and O(1)-incremental (event-time EWMA). See
docs/design/05-social-analytics.md. These are *features*, never standalone
triggers: sentiment is a denoised input, the contrarian signal is a price-gated
risk flag, the pump score is a veto.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

GAMMA = 1.5            # credibility exponent (sharpen quality advantage)
K_SHRINK = 2.0         # Bayesian prior weight (pull thin coverage to neutral)
HALFLIFE_S = 6 * 3600  # 6h half-life

_STANCE_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0, "unclear": 0}
_TIER_PRIOR = {"HOT": 0.6, "WARM": 0.35, "COLD": 0.15}


def credibility_prior(tier: str) -> float:
    """Tier-based credibility used until the M4 feedback loop produces scores."""
    return _TIER_PRIOR.get(tier, 0.15)


@dataclass
class _SymState:
    a: float = 0.0   # signed weighted sum
    b: float = 0.0   # weight sum
    q: float = 0.0   # sum of squared weights (for N_eff)
    last_ts: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=300))


class SentimentAggregator:
    """Per-symbol credibility-weighted EWMA of signed stance."""

    def __init__(self, *, gamma: float = GAMMA, k: float = K_SHRINK, halflife_s: int = HALFLIFE_S) -> None:
        self._g = gamma
        self._k = k
        self._lam = math.log(2) / halflife_s
        self._state: dict[str, _SymState] = {}

    def update(self, *, symbol: str, stance: str, stance_confidence: float, credibility: float, ts: float) -> None:
        sign = _STANCE_SIGN.get(stance, 0)
        st = self._state.setdefault(symbol, _SymState(last_ts=ts))
        decay = math.exp(-self._lam * max(0.0, ts - st.last_ts))
        w0 = (max(credibility, 0.0) ** self._g) * max(stance_confidence, 0.0)
        st.a = st.a * decay + w0 * sign
        st.b = st.b * decay + w0
        st.q = st.q * decay * decay + w0 * w0
        st.last_ts = ts
        st.history.append(self.read(symbol, ts)["S"])

    def read(self, symbol: str, now: float) -> dict:
        st = self._state.get(symbol)
        if st is None:
            return {"S": 0.0, "conf": 0.0, "n_eff": 0.0, "weight": 0.0}
        d = math.exp(-self._lam * max(0.0, now - st.last_ts))
        num, den, q = st.a * d, st.b * d, st.q * d * d
        denom = den + self._k
        return {
            "S": round(num / denom, 4) if denom else 0.0,
            "conf": round(den / denom, 4) if denom else 0.0,
            "n_eff": round((den * den / q), 4) if q > 0 else 0.0,
            "weight": den,
        }

    def history(self, symbol: str) -> list[float]:
        st = self._state.get(symbol)
        return list(st.history) if st else []

    def symbols(self) -> list[str]:
        return list(self._state)


def contrarian_signal(
    history: list[float],
    s_now: float,
    n_eff: float,
    *,
    z_hi: float = 3.0,
    p_hi: float = 0.95,
    n_min: int = 8,
    min_abs: float = 0.3,
) -> dict | None:
    """Detect a sentiment *extreme vs its own history* (not a level). A state, not
    a trade — the caller must add price confirmation before fading."""
    if len(history) < 30 or n_eff < n_min:
        return None
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history]) or 1e-9
    z = 0.6745 * (s_now - med) / mad
    p = sum(1 for x in history if x <= s_now) / len(history)
    if p >= p_hi and z >= z_hi and s_now >= min_abs:
        return {"state": "euphoria", "side": "contrarian_short", "z": round(z, 2), "p": round(p, 3)}
    if p <= (1 - p_hi) and z <= -z_hi and s_now <= -min_abs:
        return {"state": "capitulation", "side": "contrarian_long", "z": round(z, 2), "p": round(p, 3)}
    return None


def _clip01(x: float, cap: float = 6.0) -> float:
    return max(0.0, min(x, cap)) / cap


def coordinated_pump_score(
    mentions: list[dict],
    baseline_rate: float,
    *,
    z_burst_min: float = 4.0,
    c_low: float = 0.2,
) -> dict:
    """Multi-feature coordinated-pump detector. ``mentions`` carry per-account
    features (credibility, account_age_days, cluster_id, known_pumper,
    content_dup). Gated on a Poisson burst so it never fires on a quiet ticker."""
    n = len(mentions)
    if n == 0:
        return {"score": 0.0, "flag": None, "reason": "empty"}
    mu = max(baseline_rate, 1.0)
    surprise = (n - mu) / math.sqrt(mu)
    if surprise < z_burst_min:
        return {"score": 0.0, "flag": None, "reason": "no_burst", "surprise": round(surprise, 2)}

    low_cred = sum(1 for m in mentions if m.get("credibility", 0.0) < c_low) / n
    new_acct = sum(1 for m in mentions if m.get("account_age_days", 9999) < 30) / n
    dup = sum(1 for m in mentions if m.get("content_dup")) / n
    known = sum(1 for m in mentions if m.get("known_pumper")) / n
    clusters = [m["cluster_id"] for m in mentions if m.get("cluster_id") is not None]
    clu_conc = (max(Counter(clusters).values()) / n) if clusters else 0.0

    x = (
        -3.0
        + 1.0 * _clip01(surprise)
        + 1.5 * low_cred
        + 1.0 * new_acct
        + 1.2 * dup
        + 1.5 * clu_conc
        + 2.0 * known
    )
    score = 1.0 / (1.0 + math.exp(-x))
    flag = "ACT" if score >= 0.85 else "ALERT" if score >= 0.70 else None
    return {
        "score": round(score, 3),
        "flag": flag,
        "surprise": round(surprise, 2),
        "low_cred": round(low_cred, 2),
        "cluster_conc": round(clu_conc, 2),
        "known": round(known, 2),
    }
