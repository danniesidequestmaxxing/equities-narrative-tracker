"""M3: market-data gates, fail-closed runner, candidate building, recommendations."""

from datetime import datetime, timedelta, timezone

from narrative_tracker.enrich.market_data import FakeMarketData, MarketSnapshot
from narrative_tracker.notify.telegram_bot import build_call
from narrative_tracker.recommend.engine import build_candidate, recommend
from narrative_tracker.recommend.gates import run_gates
from narrative_tracker.recommend.types import Candidate, GateContext, RiskConfig
from narrative_tracker.schemas.call import Direction, Targets, TradeCall

NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)
CFG = RiskConfig()


def snap(symbol="NVDA", *, price=150.0, adv=5e8, spread=0.1, mcap=3e12, atr=3.0,
         halted=False, session_open=True, catalyst=None, age_s=10, asset="equity"):
    return MarketSnapshot(
        symbol=symbol, asset_class=asset, price=price, adv_usd=adv, spread_pct=spread,
        market_cap=mcap, atr=atr, as_of=NOW - timedelta(seconds=age_s),
        halted=halted, session_open=session_open, catalyst_within_h=catalyst,
    )


def inp(symbol="NVDA", *, stance="bullish", confidence=0.9, stance_conf=0.85, neg=False,
        extracted=None, net=0.7, pump=0.1, narrative="AI infrastructure", asset="equity"):
    return {
        "symbol": symbol, "asset_class": asset, "stance": stance, "confidence": confidence,
        "stance_confidence": stance_conf, "negation_flag": neg,
        "extracted_symbols": extracted or {symbol}, "net_cred_weighted_stance": net,
        "pump_score": pump, "narrative": narrative, "source_accounts": ["whale_trader"],
    }


def test_build_candidate_long_ordering():
    c = build_candidate(snapshot=snap(), config=CFG, inp=inp())
    assert c.direction is Direction.LONG and c.stop < c.entry < c.target


def test_build_candidate_short_ordering():
    c = build_candidate(snapshot=snap(), config=CFG, inp=inp(stance="bearish", net=-0.7))
    assert c.direction is Direction.SHORT and c.stop > c.entry > c.target


async def test_recommend_clean_candidate_emits_call():
    calls, evals = await recommend([inp()], provider=FakeMarketData({"NVDA": snap()}), config=CFG, now=NOW)
    assert len(calls) == 1 and calls[0].symbol == "NVDA"
    assert calls[0].rr == 2.0 and evals[0]["passed"] is True


async def test_recommend_halted_suppressed():
    calls, evals = await recommend([inp()], provider=FakeMarketData({"NVDA": snap(halted=True)}), config=CFG, now=NOW)
    assert calls == [] and "tradeability" in evals[0]["suppress_reason"]


async def test_recommend_illiquid_microcap_suppressed():
    prov = FakeMarketData({"PENNY": snap(symbol="PENNY", price=2.0, adv=1e5, mcap=1e7)})
    calls, evals = await recommend([inp(symbol="PENNY", narrative=None)], provider=prov, config=CFG, now=NOW)
    assert calls == [] and "tradeability" in evals[0]["suppress_reason"]


async def test_recommend_provenance_blocks_hallucinated_symbol():
    prov = FakeMarketData({"FAKE": snap(symbol="FAKE")})
    calls, evals = await recommend(
        [inp(symbol="FAKE", extracted={"NVDA"}, narrative=None)], provider=prov, config=CFG, now=NOW
    )
    assert calls == [] and "provenance" in evals[0]["suppress_reason"]


async def test_recommend_coordinated_pump_suppressed():
    prov = FakeMarketData({"PUMP": snap(symbol="PUMP")})
    calls, evals = await recommend(
        [inp(symbol="PUMP", pump=0.95, narrative=None)], provider=prov, config=CFG, now=NOW
    )
    assert calls == [] and "pump" in evals[0]["suppress_reason"]


async def test_recommend_stale_snapshot_suppressed():
    calls, evals = await recommend([inp()], provider=FakeMarketData({"NVDA": snap(age_s=2000)}), config=CFG, now=NOW)
    assert calls == [] and "staleness" in evals[0]["suppress_reason"]


async def test_recommend_correlation_cap_one_per_narrative():
    prov = FakeMarketData({"NVDA": snap("NVDA"), "AMD": snap("AMD", price=160, mcap=2e11)})
    calls, _ = await recommend([inp("NVDA"), inp("AMD")], provider=prov, config=CFG, now=NOW, max_calls=3)
    assert len(calls) == 1  # both are "AI infrastructure"


class _BadSnap:
    asset_class = "equity"
    price = 100.0
    halted = False
    adv_usd = 1e9
    spread_pct = 0.1
    market_cap = 1e12
    optionable = True
    session_open = True
    catalyst_within_h = None

    def age_s(self, now):
        raise RuntimeError("boom")


def test_run_gates_is_fail_closed_on_exception():
    cand = Candidate(
        symbol="X", asset_class="equity", direction=Direction.LONG, entry=100, stop=95,
        target=110, confidence=0.9, stance="bullish", stance_confidence=0.8,
        negation_flag=False, extracted_symbols={"X"}, net_cred_weighted_stance=0.7,
        pump_score=0.1, narrative=None,
    )
    ctx = GateContext(snapshot=_BadSnap(), config=CFG, now=NOW)
    passed, results = run_gates(cand, ctx)
    assert passed is False
    assert any(r.name == "g_staleness" and not r.passed for r in results)


def test_build_call_message_has_disclaimer_and_link():
    call = TradeCall(
        call_id="CALL-2026-06-03-NVDA-001", symbol="NVDA", asset_class="equity",
        direction=Direction.LONG, entry=150.0, stop=145.5, targets=Targets(t1=159.0),
        size_hint="1.0% NAV", horizon="swing · 1-3w", confidence=0.85,
        narrative="AI infrastructure", source_accounts=["whale_trader"],
    )
    mdv2, plain = build_call(call)
    assert "NOT FINANCIAL ADVICE" in mdv2 and "tradingview.com" in mdv2
    assert "NVDA" in plain and "CALL" in plain
