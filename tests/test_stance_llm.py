"""LLM stance path + fail-safe fallback (no credentials — fakes stand in for the API)."""

from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.extract.stance import (
    FallbackStanceClassifier,
    LlmStanceClassifier,
    RuleBasedStanceClassifier,
    StanceLabel,
    build_stance_classifier,
)
from narrative_tracker.schemas.mention import Stance

ANSEM = "$MRVL up ~50% in a day since Jensen said it was the next trillion dollar stock"


async def test_llm_classifier_returns_label():
    async def fake_infer(text):
        return StanceLabel(stance=Stance.BULLISH, negation=False, confidence=0.88)

    r = await LlmStanceClassifier(infer=fake_infer).classify(ANSEM)
    assert r.stance is Stance.BULLISH and r.stance_confidence == 0.88


async def test_fallback_uses_rulebased_when_llm_fails():
    async def boom(text):
        raise RuntimeError("LLM over budget")

    clf = FallbackStanceClassifier(LlmStanceClassifier(infer=boom), RuleBasedStanceClassifier())
    # The rule-based path handles negation deterministically.
    r = await clf.classify("$NVDA is NOT a buy")
    assert r.stance is Stance.BEARISH


async def test_build_stance_classifier_defaults_to_rulebased():
    assert isinstance(build_stance_classifier(model=None), RuleBasedStanceClassifier)


async def test_pipeline_with_llm_marks_momentum_tweet_bullish():
    """The exact case the rule-based engine misses: momentum-observation language."""

    async def fake_llm(text):
        return StanceLabel(stance=Stance.BULLISH, negation=False, confidence=0.9)

    pipe = ExtractionPipeline(stance=LlmStanceClassifier(infer=fake_llm))
    mentions = await pipe.extract(text=ANSEM)
    assert mentions and all(m.stance is Stance.BULLISH for m in mentions)
    assert all(m.stance_confidence == 0.9 for m in mentions)


async def test_rulebased_alone_is_neutral_on_momentum_tweet():
    """Documents the gap the LLM fills: no explicit buy-word -> neutral."""
    pipe = ExtractionPipeline()  # rule-based default
    mentions = await pipe.extract(text=ANSEM)
    assert mentions and all(m.stance is Stance.NEUTRAL for m in mentions)
