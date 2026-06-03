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

import re
from dataclasses import dataclass
from typing import Protocol

from ..schemas.mention import Stance


@dataclass(frozen=True)
class StanceResult:
    stance: Stance
    negation_flag: bool
    stance_confidence: float


class StanceClassifier(Protocol):
    def classify(self, text: str, symbol: str | None = None) -> StanceResult: ...


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
    """Deterministic stance + negation detection."""

    def classify(self, text: str, symbol: str | None = None) -> StanceResult:
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


def build_llm_stance_classifier(model: str) -> StanceClassifier:  # pragma: no cover
    """Construct the LLM-backed classifier (lazy; part of the ``prod`` extra).

    Falls back to the rule-based classifier if the LLM client is unavailable —
    the fail-safe degraded mode.
    """
    raise NotImplementedError(
        "LLM stance classifier is wired in a later milestone; "
        "M1 uses RuleBasedStanceClassifier."
    )
