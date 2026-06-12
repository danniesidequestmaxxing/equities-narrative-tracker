"""M14: LLM mention inference for cashtag-less ticker talk."""

from datetime import datetime, timezone

from narrative_tracker.extract.mentions_llm import InferredMentions, InferredTicker, to_mentions
from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.ingest.provider import RawPost
from narrative_tracker.notify.telegram_bot import build_alert
from narrative_tracker.schemas.mention import ResolutionMethod, Stance

T0 = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


def test_to_mentions_filters_and_caps():
    read = InferredMentions(has_tickers=True, items=[
        InferredTicker(symbol="$mu", asset_class="equity", confidence=0.9),
        InferredTicker(symbol="BTC", asset_class="crypto", confidence=0.95),
        InferredTicker(symbol="MAYBE", confidence=0.4),         # below threshold
        InferredTicker(symbol="NOTATICKER", confidence=0.9),    # >6 chars
    ])
    out = to_mentions(read, source_post_id="p1")
    assert [m.symbol for m in out] == ["MU", "BTC"]
    assert out[0].resolution_method is ResolutionMethod.LLM_INFERENCE
    assert out[0].mention_confidence <= 0.85                    # capped below cashtags
    assert out[1].asset_class.value == "crypto"
    assert to_mentions(InferredMentions(has_tickers=False)) == []


class FakeInferrer:
    def __init__(self):
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        if "Micron" in text:
            return InferredMentions(has_tickers=True, items=[
                InferredTicker(symbol="MU", confidence=0.9)])
        return InferredMentions(has_tickers=False)


async def test_pipeline_infers_only_when_deterministic_finds_nothing():
    inf = FakeInferrer()
    pipe = ExtractionPipeline(inferrer=inf)

    # cashtag-less but clearly about MU -> inferred, and stance still applies
    mentions = await pipe.extract(text="Micron is going to rip after earnings, loading calls")
    assert [m.symbol for m in mentions] == ["MU"]
    assert mentions[0].stance is Stance.BULLISH        # S3 stance pass ran on it
    assert mentions[0].resolution_method is ResolutionMethod.LLM_INFERENCE

    # cashtag present -> deterministic path wins, inferrer NOT consulted
    n_calls = len(inf.calls)
    out = await pipe.extract(text="$NVDA breaking out")
    assert [m.symbol for m in out] == ["NVDA"]
    assert len(inf.calls) == n_calls

    # too-short noise -> inferrer not consulted
    await pipe.extract(text="gm \U0001F680")
    assert len(inf.calls) == n_calls


async def test_pipeline_inference_fail_open():
    class Broken:
        async def __call__(self, text):
            raise RuntimeError("llm down")

    out = await ExtractionPipeline(inferrer=Broken()).extract(
        text="Micron is going to rip after earnings")
    assert out == []  # no crash, just no mentions


def test_alert_tags_inferred_mentions():
    from narrative_tracker.schemas.mention import AssetClass, Mention

    post = RawPost(platform_user_id="whale", handle="whale", platform_post_id="1",
                   text="Micron is going to rip", posted_at=T0)
    m = Mention(symbol="MU", asset_class=AssetClass.EQUITY,
                resolution_method=ResolutionMethod.LLM_INFERENCE,
                mention_confidence=0.8, stance=Stance.BULLISH, stance_confidence=0.8)
    mdv2, plain = build_alert(post, m)
    assert "inferred" in mdv2 and "inferred" in plain
