"""Stance + negation classification (M1).

Stance is the AUTHOR'S directional view, not surface polarity — "$X is NOT a buy"
is bearish, not bullish-because-"buy"-appears. The rule-based classifier here is:

* the fully-testable M1 path (no credentials), and
* the fail-safe **degraded mode** when the LLM is down / over budget (the plan's
  fail-closed posture) — extraction continues at reduced confidence rather than
  stopping.

The production path (``LlmStanceClassifier``) is an LLM with the instruction-rich
prompt from docs/design/04; it conforms to the same ``StanceClassifier`` protocol.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel, Field

from ..schemas.mention import Stance

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StanceResult:
    stance: Stance
    negation_flag: bool
    stance_confidence: float


class StanceClassifier(Protocol):
    async def classify(self, text: str, symbol: str | None = None) -> StanceResult: ...


_BULLISH = {
    "buy", "buying", "long", "calls", "call", "bullish", "breakout", "rip",
    "ripping", "send", "moon", "load", "loaded", "loading", "accumulate",
    "accumulating", "strong", "undervalued", "lfg", "bottom", "squeeze",
    "higher", "bid", "ripped", "rally",
}
_BEARISH = {
    "sell", "selling", "short", "puts", "put", "bearish", "dump", "dumping",
    "crash", "overvalued", "weak", "avoid", "fade", "fading", "rug", "tanking",
    "puke", "dead", "lower", "rekt", "rolling",
}
_NEGATORS = {
    "not", "no", "never", "isn't", "aint", "ain't", "wouldn't", "won't",
    "don't", "cant", "can't", "dont", "wont",
}
_SARCASM = (
    "imagine being long", "imagine buying", "what could go wrong",
    "great job buying", "buying the top", "totally not financial advice",
    "\U0001f480", "\U0001f921",  # 💀 🤡
)


def _flip(stance: Stance) -> Stance:
    if stance is Stance.BULLISH:
        return Stance.BEARISH
    if stance is Stance.BEARISH:
        return Stance.BULLISH
    return stance


class RuleBasedStanceClassifier:
    """Deterministic stance + negation detection (also the fail-safe fallback)."""

    async def classify(self, text: str, symbol: str | None = None) -> StanceResult:
        low = (text or "").lower()
        if low.strip().endswith("?"):
            return StanceResult(Stance.NEUTRAL, False, 0.8)

        tokens = set(re.findall(r"[a-z']+", low))
        bull = len(tokens & _BULLISH) + low.count("\U0001f680")  # 🚀
        bear = len(tokens & _BEARISH)
        has_neg = bool(tokens & _NEGATORS) or "n't" in low
        sarcasm = any(marker in low for marker in _SARCASM)

        if bull == 0 and bear == 0:
            # No explicit lexicon — recover an implicit direction for sarcasm.
            if "long" in low or "buying" in low or "\U0001f680" in low:
                base, strength = Stance.BULLISH, 0.5
            elif "short" in low or "selling" in low:
                base, strength = Stance.BEARISH, 0.5
            else:
                stance = Stance.UNCLEAR if sarcasm else Stance.NEUTRAL
                return StanceResult(stance, has_neg, 0.4 if sarcasm else 0.55)
        elif bull > bear:
            base, strength = Stance.BULLISH, min(1.0, 0.55 + 0.15 * (bull - bear))
        elif bear > bull:
            base, strength = Stance.BEARISH, min(1.0, 0.55 + 0.15 * (bear - bull))
        else:
            return StanceResult(Stance.UNCLEAR, has_neg, 0.4)

        neg_flag = False
        if has_neg and base in (Stance.BULLISH, Stance.BEARISH):
            base = _flip(base)
            neg_flag = True
        if sarcasm and base in (Stance.BULLISH, Stance.BEARISH):
            base = _flip(base)
            strength = min(strength, 0.65)

        return StanceResult(base, neg_flag, round(strength, 2))


STANCE_SYSTEM_PROMPT = """\
You label the AUTHOR'S directional stance toward the tickers in one X/Twitter post.
Stance is about how the author is positioned / what they want the price to do — NOT
the surface polarity of individual words.

Rules:
1. NEGATION: detect negation and its scope. "$X is NOT a buy" is bearish/neutral,
   never bullish. Set negation=true and explain in one phrase.
2. SARCASM/IRONY: "imagine being long $X here", "great job buying the top", 💀, 🤡
   usually INVERT the literal meaning and LOWER confidence.
3. MOMENTUM FRAMING IS BULLISH: "up ~50%", "next trillion dollar stock", "up 2x",
   "ripping", "parabolic", "ATH" express a bullish view even without the word "buy".
4. Pure questions or news with no opinion => neutral.
5. If you genuinely cannot tell => unclear. Do not guess high confidence.
Return calibrated confidence in [0,1]."""


class StanceLabel(BaseModel):
    """Schema the LLM is constrained to fill (provider-native structured output)."""

    stance: Stance
    negation: bool = False
    confidence: float = Field(ge=0, le=1)


class LlmStanceClassifier:
    """LLM-backed stance. ``infer`` is an async callable (text -> StanceLabel); the
    production builder wires it to ``instructor`` + a provider, tests inject a fake."""

    def __init__(self, *, infer: Callable[[str], Awaitable[StanceLabel]]) -> None:
        self._infer = infer

    async def classify(self, text: str, symbol: str | None = None) -> StanceResult:
        label = await self._infer(text)
        return StanceResult(
            stance=label.stance,
            negation_flag=bool(label.negation),
            stance_confidence=float(label.confidence),
        )


class FallbackStanceClassifier:
    """Try the primary (LLM); on ANY failure (no key, API error, over budget,
    validation error) fall back to the deterministic classifier. This is the
    plan's fail-safe degraded mode for stance."""

    def __init__(self, primary: StanceClassifier, fallback: StanceClassifier) -> None:
        self._primary = primary
        self._fallback = fallback

    async def classify(self, text: str, symbol: str | None = None) -> StanceResult:
        try:
            return await self._primary.classify(text, symbol)
        except Exception as exc:  # noqa: BLE001 - degrade, never break extraction
            log.warning("LLM stance failed (%s); falling back to rule-based", exc)
            return await self._fallback.classify(text, symbol)


def build_llm_infer(model: str) -> Callable[[str], Awaitable[StanceLabel]]:  # pragma: no cover
    """Wire an async LLM inference callable via instructor (part of the ``prod`` extra)."""

    async def infer(text: str) -> StanceLabel:
        import instructor  # lazy

        client = instructor.from_provider(model, async_client=True)
        return await client.chat.completions.create(
            response_model=StanceLabel,
            max_retries=2,
            messages=[
                {"role": "system", "content": STANCE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )

    return infer


def build_stance_classifier(*, model: str | None = None) -> StanceClassifier:
    """Factory: LLM (+ rule-based fallback) when a model is configured, else
    the deterministic rule-based classifier."""
    rule = RuleBasedStanceClassifier()
    if not model:
        return rule
    return FallbackStanceClassifier(LlmStanceClassifier(infer=build_llm_infer(model)), rule)
