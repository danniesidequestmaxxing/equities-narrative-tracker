"""End-to-end extraction pipeline (M1 exit criteria)."""

from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.extract.vision import (
    CachingBudgetedVision,
    CountBudget,
    FakeVisionExtractor,
)
from narrative_tracker.schemas.mention import (
    AssetClass,
    Mention,
    OptionRight,
    ResolutionMethod,
    Stance,
)


async def test_pipeline_cashtag_with_stance():
    mentions = await ExtractionPipeline().extract(text="long $NVDA; $4200 spx level")
    assert {m.symbol for m in mentions} == {"NVDA"}  # $4200 filtered
    assert mentions[0].stance is Stance.BULLISH


async def test_pipeline_cashtagless_resolution():
    mentions = await ExtractionPipeline().extract(
        text="Nvidia keeps printing, and the Ozempic maker too"
    )
    assert {"NVDA", "NVO"} <= {m.symbol for m in mentions}


async def test_pipeline_not_a_buy_is_bearish():
    mentions = await ExtractionPipeline().extract(text="$NVDA is NOT a buy up here")
    assert mentions[0].stance is Stance.BEARISH
    assert mentions[0].negation_flag is True


async def test_pipeline_option_supersedes_equity():
    mentions = await ExtractionPipeline().extract(text="grabbing $SPY 600c 0DTE")
    options = [m for m in mentions if m.asset_class is AssetClass.OPTION]
    assert options and options[0].symbol == "SPY"
    assert options[0].option_detail.right is OptionRight.CALL
    # SPY must NOT also appear as a bare equity.
    assert not any(
        m.symbol == "SPY" and m.asset_class is AssetClass.EQUITY for m in mentions
    )


async def test_pipeline_collision_word_dropped():
    mentions = await ExtractionPipeline().extract(text="$ON and then walk on by")
    assert all(m.symbol != "ON" for m in mentions)


async def test_pipeline_vision_for_image_only_post():
    img = "https://pbs.twimg.com/media/chart.jpg"
    fake = FakeVisionExtractor(
        {
            img: [
                Mention(
                    symbol="GME",
                    asset_class=AssetClass.EQUITY,
                    resolution_method=ResolutionMethod.VISION_OCR,
                    mention_confidence=0.7,
                )
            ]
        }
    )
    mentions = await ExtractionPipeline(vision=fake).extract(
        text="look at this chart \U0001f447", media_urls=[img]
    )
    assert any(
        m.symbol == "GME" and m.resolution_method is ResolutionMethod.VISION_OCR
        for m in mentions
    )


async def test_vision_cache_avoids_second_call():
    img = "https://x/chart.jpg"
    fake = FakeVisionExtractor(
        {img: [Mention(symbol="AMC", asset_class=AssetClass.EQUITY, resolution_method=ResolutionMethod.VISION_OCR)]}
    )
    cached = CachingBudgetedVision(fake)
    await cached.extract(img)
    await cached.extract(img)
    assert fake.calls == 1  # second call served from cache


async def test_vision_budget_denies_when_exhausted():
    a, b = "https://x/a.jpg", "https://x/b.jpg"
    fake = FakeVisionExtractor(
        {
            a: [Mention(symbol="A", asset_class=AssetClass.EQUITY, resolution_method=ResolutionMethod.VISION_OCR)],
            b: [Mention(symbol="B", asset_class=AssetClass.EQUITY, resolution_method=ResolutionMethod.VISION_OCR)],
        }
    )
    cached = CachingBudgetedVision(fake, budget=CountBudget(1))
    first = await cached.extract(a)
    second = await cached.extract(b)  # over budget -> degraded
    assert first and second == []
