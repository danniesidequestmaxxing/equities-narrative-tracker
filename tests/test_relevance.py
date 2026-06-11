"""LLM relevance gate: drop cashtags that aren't about the asset ($RDDT incident)."""

import pytest

from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.extract.relevance import LlmRelevanceGate, RelevanceVerdict, TickerRelevance

# The real tweet that fired the bogus alert: $RDDT here means Reddit-the-site.
INCIDENT = "@Ud197601 Everyone kept calling $AXTI a scam, and my thesis BS on $RDDT.\n\nGlad to see my thesis being validated by Reuters and Chinese/US trade negotiations lol."


class FakeGate:
    def __init__(self, verdicts: dict, fail: bool = False):
        self._verdicts = verdicts
        self._fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    async def check(self, text, symbols):
        self.calls.append((text, list(symbols)))
        if self._fail:
            raise RuntimeError("LLM down")
        return self._verdicts


def _verdict(symbol, ok, conf, reason=""):
    return TickerRelevance(symbol=symbol, is_asset_reference=ok, confidence=conf, reason=reason)


async def test_gate_drops_colloquial_cashtag_keeps_asset():
    gate = FakeGate({
        "AXTI": _verdict("AXTI", True, 0.95),
        "RDDT": _verdict("RDDT", False, 0.9, "RDDT = Reddit the website here"),
    })
    mentions = await ExtractionPipeline(relevance=gate).extract(text=INCIDENT)
    assert {m.symbol for m in mentions} == {"AXTI"}
    assert gate.calls and set(gate.calls[0][1]) == {"AXTI", "RDDT"}  # one call, all symbols


async def test_gate_failure_keeps_all_mentions():
    mentions = await ExtractionPipeline(relevance=FakeGate({}, fail=True)).extract(text=INCIDENT)
    assert {m.symbol for m in mentions} == {"AXTI", "RDDT"}  # fail-open


async def test_low_confidence_non_asset_is_kept():
    gate = FakeGate({"RDDT": _verdict("RDDT", False, 0.4, "unsure")})
    mentions = await ExtractionPipeline(relevance=gate).extract(text="$RDDT to the moon")
    assert {m.symbol for m in mentions} == {"RDDT"}  # borderline -> keep


async def test_no_gate_means_no_change():
    mentions = await ExtractionPipeline().extract(text=INCIDENT)
    assert {m.symbol for m in mentions} == {"AXTI", "RDDT"}


async def test_gate_not_called_without_mentions():
    gate = FakeGate({})
    await ExtractionPipeline(relevance=gate).extract(text="nothing ticker-shaped here")
    assert gate.calls == []


async def test_llm_gate_maps_verdicts_by_symbol():
    async def infer(text, symbols):
        return RelevanceVerdict(tickers=[
            TickerRelevance(symbol="$rddt", is_asset_reference=False, confidence=0.8),
            TickerRelevance(symbol="AXTI", is_asset_reference=True, confidence=0.9),
        ])

    out = await LlmRelevanceGate(infer=infer).check("text", ["AXTI", "RDDT"])
    assert out["RDDT"].is_asset_reference is False  # normalized: $rddt -> RDDT
    assert out["AXTI"].is_asset_reference is True
