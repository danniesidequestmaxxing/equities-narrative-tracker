"""The pre-broadcast gate chain (M3).

The LLM/sentiment layer is an *untrusted proposer*: every field of a candidate is
re-checked against system-of-record data here. A candidate broadcasts only if
**every** gate returns an explicit pass. The runner is **fail-closed** — any gate
that raises is treated as a failure and suppresses the call (not just a fail
verdict). Every evaluation is returned for the ``gate_evaluations`` audit log.

See docs/plans (Pre-Broadcast Gate Chain) — gates 1–14.
"""

from __future__ import annotations

from collections.abc import Callable

from ..schemas.call import Direction
from .types import Candidate, GateContext, GateResult


def g_confidence(c: Candidate, ctx: GateContext) -> GateResult:
    ok = (
        c.confidence >= ctx.config.theta_call
        and c.stance_confidence >= ctx.config.phi_call
        and c.stance in ("bullish", "bearish")
    )
    return GateResult("confidence", ok, {"conf": c.confidence, "stance_conf": c.stance_confidence, "stance": c.stance})


def g_negation(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult("negation", not c.negation_flag, {"negation": c.negation_flag})


def g_provenance(c: Candidate, ctx: GateContext) -> GateResult:
    # The call's symbol must actually appear in the source posts (anti-hallucination).
    return GateResult("provenance", c.symbol in c.extracted_symbols, {"symbol": c.symbol})


def g_numeric_sanity(c: Candidate, ctx: GateContext) -> GateResult:
    s = ctx.snapshot
    within = abs(c.entry - s.price) / s.price * 100 <= ctx.config.numeric_sanity_pct if s.price else False
    if c.direction is Direction.LONG:
        ordered = c.stop < c.entry < c.target
    else:
        ordered = c.stop > c.entry > c.target
    return GateResult("numeric_sanity", bool(within and ordered), {"within_pct": within, "ordered": ordered})


def g_tradeability(c: Candidate, ctx: GateContext) -> GateResult:
    s, cfg = ctx.snapshot, ctx.config
    ok = (
        not s.halted
        and s.price >= cfg.pmin
        and s.adv_usd >= cfg.vmin_usd
        and s.spread_pct <= cfg.smax_pct
        and s.market_cap >= cfg.min_market_cap
    )
    if c.asset_class == "option":
        ok = ok and s.optionable
    return GateResult("tradeability", ok, {"halted": s.halted, "price": s.price, "adv_usd": s.adv_usd, "spread_pct": s.spread_pct})


def g_staleness(c: Candidate, ctx: GateContext) -> GateResult:
    s = ctx.snapshot
    limit = ctx.config.staleness_crypto_s if c.asset_class == "crypto" else ctx.config.staleness_equity_s
    age = s.age_s(ctx.now)
    return GateResult("staleness", age <= limit, {"age_s": age, "limit_s": limit})


def g_session(c: Candidate, ctx: GateContext) -> GateResult:
    s = ctx.snapshot
    ok = True if c.asset_class == "crypto" else s.session_open
    return GateResult("session", ok, {"session_open": s.session_open})


def g_catalyst(c: Candidate, ctx: GateContext) -> GateResult:
    s = ctx.snapshot
    ok = s.catalyst_within_h is None or s.catalyst_within_h > ctx.config.catalyst_h
    return GateResult("catalyst", ok, {"catalyst_within_h": s.catalyst_within_h})


def g_conflict(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult(
        "conflict", abs(c.net_cred_weighted_stance) >= ctx.config.conflict_eps, {"net": c.net_cred_weighted_stance}
    )


def g_pump(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult("pump", c.pump_score < ctx.config.pump_act_threshold, {"pump_score": c.pump_score})


def g_correlation(c: Candidate, ctx: GateContext) -> GateResult:
    ok = c.narrative is None or c.narrative not in ctx.selected_narratives
    return GateResult("correlation", ok, {"narrative": c.narrative})


def g_stacking(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult("stacking", c.symbol not in ctx.live_call_symbols, {"symbol": c.symbol})


def g_budget(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult("budget", ctx.budget_ok, {"budget_ok": ctx.budget_ok})


def g_audit(c: Candidate, ctx: GateContext) -> GateResult:
    return GateResult(
        "audit", ctx.audit_writable and not ctx.killswitch_engaged,
        {"audit_writable": ctx.audit_writable, "killswitch": ctx.killswitch_engaged},
    )


# Ordered registry — easy to reorder, tune, and unit-test in isolation.
GATES: list[Callable[[Candidate, GateContext], GateResult]] = [
    g_confidence,
    g_negation,
    g_provenance,
    g_numeric_sanity,
    g_tradeability,
    g_staleness,
    g_session,
    g_catalyst,
    g_conflict,
    g_pump,
    g_correlation,
    g_stacking,
    g_budget,
    g_audit,
]


def run_gates(c: Candidate, ctx: GateContext) -> tuple[bool, list[GateResult]]:
    """Evaluate every gate (full forensics). Fail-closed: a gate that raises is a
    failure. Broadcasts only if every gate returned an explicit pass."""
    results: list[GateResult] = []
    for gate in GATES:
        try:
            results.append(gate(c, ctx))
        except Exception as exc:  # noqa: BLE001 - gate runner is fail-closed
            results.append(GateResult(gate.__name__, False, {"error": str(exc)}))
    passed = len(results) == len(GATES) and all(r.passed for r in results)
    return passed, results
