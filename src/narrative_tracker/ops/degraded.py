"""Degraded-mode posture (M5).

Maps dependency health to what the system is allowed to do. **Fail-closed**: live
calls are suppressed whenever audit (Postgres), fresh prices (market data), the
budget, or the LLM are unavailable. Alerts may continue degraded (cashtag-only)
as long as Postgres + Telegram are up.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DependencyHealth:
    postgres: bool = True
    market_data: bool = True
    llm: bool = True
    telegram: bool = True
    redis: bool = True
    budget_ok: bool = True


@dataclass
class Posture:
    can_ingest: bool
    can_alert: bool
    can_broadcast: bool
    reason: str


def posture(h: DependencyHealth) -> Posture:
    if not h.postgres:
        # No audit, no idempotency authority -> suppress everything broadcast-side.
        return Posture(can_ingest=False, can_alert=False, can_broadcast=False, reason="postgres down: no audit -> suppress all")

    reasons: list[str] = []
    can_broadcast = True
    if not h.market_data:
        can_broadcast = False
        reasons.append("stale/no market data -> no calls")
    if not h.budget_ok:
        can_broadcast = False
        reasons.append("budget exhausted -> no calls")
    if not h.llm:
        can_broadcast = False
        reasons.append("LLM down -> cashtag-only, no calls")
    if not h.telegram:
        can_broadcast = False
        reasons.append("telegram down -> queue, escalate to ops")

    can_alert = h.telegram  # alerts can continue (degraded) without market data / LLM
    return Posture(
        can_ingest=True,
        can_alert=can_alert,
        can_broadcast=can_broadcast,
        reason="; ".join(reasons) or "healthy",
    )
