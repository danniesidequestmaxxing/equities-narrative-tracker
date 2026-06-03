"""Recommendation engine (M3): build candidates, rank, gate, emit calls."""

from __future__ import annotations

from datetime import datetime

from ..enrich.market_data import MarketDataProvider, MarketSnapshot
from ..schemas.call import Direction, Targets, TradeCall
from .gates import run_gates
from .types import Candidate, GateContext, RiskConfig


def build_candidate(*, snapshot: MarketSnapshot, config: RiskConfig, inp: dict) -> Candidate:
    """Compute entry/stop/target from price + ATR for a proto-call."""
    direction = Direction.LONG if inp["stance"] == "bullish" else Direction.SHORT
    price = snapshot.price
    atr = snapshot.atr or price * 0.02
    risk = config.atr_stop_mult * atr
    if direction is Direction.LONG:
        entry, stop, target = price, price - risk, price + config.rr_target * risk
    else:
        entry, stop, target = price, price + risk, price - config.rr_target * risk
    return Candidate(
        symbol=inp["symbol"],
        asset_class=inp["asset_class"],
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        confidence=inp["confidence"],
        stance=inp["stance"],
        stance_confidence=inp["stance_confidence"],
        negation_flag=inp.get("negation_flag", False),
        extracted_symbols=set(inp.get("extracted_symbols", {inp["symbol"]})),
        net_cred_weighted_stance=inp.get("net_cred_weighted_stance", 0.0),
        pump_score=inp.get("pump_score", 0.0),
        narrative=inp.get("narrative"),
        source_accounts=inp.get("source_accounts", []),
    )


def build_trade_call(cand: Candidate, *, config: RiskConfig, call_id: str, horizon: str, rationale: str = "") -> TradeCall:
    return TradeCall(
        call_id=call_id,
        symbol=cand.symbol,
        asset_class=cand.asset_class,
        direction=cand.direction,
        entry=round(cand.entry, 2),
        stop=round(cand.stop, 2),
        targets=Targets(t1=round(cand.target, 2)),
        size_hint=f"{config.risk_per_trade_pct:.1f}% NAV",
        horizon=horizon,
        confidence=round(cand.confidence, 2),
        rationale=rationale,
        source_accounts=cand.source_accounts,
        narrative=cand.narrative,
    )


async def recommend(
    inputs: list[dict],
    *,
    provider: MarketDataProvider,
    config: RiskConfig,
    now: datetime,
    date_label: str = "2026-06-03",
    horizon: str = "swing · 1-3w",
    live_call_symbols: set[str] | None = None,
    budget_ok: bool = True,
    audit_writable: bool = True,
    killswitch_engaged: bool = False,
    max_calls: int = 3,
) -> tuple[list[TradeCall], list[dict]]:
    """Rank candidates, run each through the gate chain, emit up to ``max_calls``.

    Returns ``(calls, gate_evaluations)`` — the evaluations are the audit trail
    (which gates passed/failed for every candidate, incl. suppressed ones).
    """
    selected_narratives: set[str] = set()
    live = set(live_call_symbols or set())
    calls: list[TradeCall] = []
    evaluations: list[dict] = []

    ranked = sorted(
        inputs,
        key=lambda x: x["confidence"] * abs(x.get("net_cred_weighted_stance", 0.0)),
        reverse=True,
    )
    seq = 0
    for inp in ranked:
        snapshot = await provider.snapshot(inp["symbol"], inp["asset_class"])
        if snapshot is None:
            evaluations.append({"symbol": inp["symbol"], "passed": False, "suppress_reason": "no_market_data", "gates": []})
            continue
        cand = build_candidate(snapshot=snapshot, config=config, inp=inp)
        ctx = GateContext(
            snapshot=snapshot,
            config=config,
            now=now,
            live_call_symbols=live,
            selected_narratives=selected_narratives,
            budget_ok=budget_ok,
            audit_writable=audit_writable,
            killswitch_engaged=killswitch_engaged,
        )
        passed, results = run_gates(cand, ctx)
        failed = [r.name for r in results if not r.passed]
        evaluations.append(
            {
                "symbol": inp["symbol"],
                "passed": passed,
                "suppress_reason": None if passed else ",".join(failed),
                "gates": [{"name": r.name, "passed": r.passed, "measured": r.measured} for r in results],
            }
        )
        if not passed:
            continue
        seq += 1
        call_id = f"CALL-{date_label}-{cand.symbol}-{seq:03d}"
        calls.append(build_trade_call(cand, config=config, call_id=call_id, horizon=horizon, rationale=inp.get("rationale", "")))
        if cand.narrative:
            selected_narratives.add(cand.narrative)
        live.add(cand.symbol)
        if len(calls) >= max_calls:
            break
    return calls, evaluations
