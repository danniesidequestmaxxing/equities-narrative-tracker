"""Credibility recomputation + multi-account attribution (M4).

Implements the moat's correctness condition: ``credibility(account, as_of=T)`` is a
**pure recomputation** over the closed-outcome set ``{ closed_at <= T }`` — never
an incremental delta, never incorporating outcomes that close after T (no
look-ahead). Attribution uses signed alignment so the system learns from
contrarians. See docs/design/02-credibility-attribution.md.
"""

from __future__ import annotations

import math
from collections import defaultdict

DAY = 86400


def attribute_call(call: dict, *, eta: float = 1.0, ha_days: float = 3.0) -> dict[str, float]:
    """Split a call's benchmark-neutral R across its contributing accounts.

    ``call`` has: ``R`` (realized), ``bench_R``, ``dir`` (+1/-1), ``open_time``,
    and ``contribs`` = [{account, stance(+1/-1), conf, mention_time}].
    Aligned accounts share the outcome; opposed accounts share its inverse (so a
    correct contrarian gains credibility, a wrong one loses it).
    """
    r_perp = call["R"] - call.get("bench_R", 0.0)
    ha = ha_days * DAY
    raw: dict[str, float] = {}
    for x in call["contribs"]:
        first_mover = 2 ** (-(call["open_time"] - x["mention_time"]) / ha) if ha else 1.0
        align = 1.0 if x["stance"] == call["dir"] else -eta
        raw[x["account"]] = x.get("conf", 1.0) * first_mover * align
    a = sum(v for v in raw.values() if v > 0)
    d = sum(-v for v in raw.values() if v < 0)
    attr: dict[str, float] = {}
    for acct, r in raw.items():
        if r >= 0 and a > 0:
            attr[acct] = r_perp * (r / a)
        elif r < 0 and d > 0:
            attr[acct] = (-r_perp) * (-r / d)
    return attr


def recompute_credibility(
    calls: list[dict],
    T: float,
    *,
    h_decay_days: float = 180.0,
    k_e: float = 10.0,
    m_reliab: float = 5.0,
    n_min: int = 2,
    theta: float = 0.7,
    prior: float = 2.0,
    floor: float = 1e-3,
) -> dict[str, float]:
    """Pure function of closed outcomes with ``closed_at <= T``."""
    closed = [c for c in calls if c.get("closed_at") is not None and c["closed_at"] <= T]
    if not closed:
        return {}

    h = h_decay_days * DAY
    samples: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for c in closed:
        w_decay = 2 ** (-(T - c["closed_at"]) / h)
        for acct, attr in attribute_call(c).items():
            samples[acct].append((attr, w_decay))

    cred: dict[str, float] = {}
    for acct, s in samples.items():
        sw = sum(w for _, w in s)
        if len(s) < n_min or sw <= 0:
            cred[acct] = floor
            continue
        wins = sum(w for r, w in s if r > 0)
        p_hat = (prior + wins) / (2 * prior + sw)              # EB Beta win-rate
        e_a = sum(r * w for r, w in s) / sw
        e_sh = (sw / (sw + k_e)) * e_a                          # shrinkage expectancy (toward 0)
        gate = sw / (sw + m_reliab)                            # reliability gate
        cred[acct] = round((p_hat ** theta) * max(e_sh, 0.0) * gate + floor, 6)
    return cred


# --- M10: evidence-weighted credibility (fuses the M9 alpha ledger) ----------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def evidence_credibility(
    tier: str,
    *,
    event_n: int = 0,
    event_edge: float | None = None,
    stated_n: int = 0,
    stated_avg_r: float | None = None,
    stated_hit: float | None = None,
) -> float:
    """Blend the tier prior with M9 evidence, shrunk by sample size.

    Two evidence streams, weakest to strongest:
    * event-study: avg direction-signed 3-day excess return per mention
      (weight grows as n/(n+8) — eight mentions earn half a say);
    * stated calls: avg realized R (or hit rate when no stops were stated) —
      each closed stated call counts double an ordinary mention, because a
      stated trade is the truest skill signal.

    With zero evidence this returns the tier prior unchanged; with mountains
    of evidence the prior washes out. Output clamped to [0.05, 0.95] so no
    account is ever silenced or deified.
    """
    from ..analyze.sentiment import credibility_prior

    score = credibility_prior(tier)
    if event_n and event_edge is not None:
        skill = 0.5 + _clamp(event_edge * 8.0, -0.35, 0.35)   # +4.4% avg edge -> ~0.85
        w = event_n / (event_n + 8.0)
        score = (1 - w) * score + w * skill
    if stated_n:
        if stated_avg_r is not None:
            skill = 0.5 + _clamp(stated_avg_r * 0.25, -0.4, 0.4)   # +1.6R avg -> 0.9
        elif stated_hit is not None:
            skill = 0.5 + _clamp((stated_hit - 0.5) * 0.8, -0.4, 0.4)
        else:
            skill = None
        if skill is not None:
            w = (2.0 * stated_n) / (2.0 * stated_n + 8.0)
            score = (1 - w) * score + w * skill
    return round(_clamp(score, 0.05, 0.95), 3)
