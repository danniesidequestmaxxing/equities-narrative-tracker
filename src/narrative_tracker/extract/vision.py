"""Vision extraction for chart-image posts (M1).

Many trader posts are chart screenshots with little/no text. A multimodal LLM
reads the ticker (and later, the setup) off the image. Two cross-cutting controls
wrap any extractor:

* **dedup cache** keyed by media-URL hash — never pay to OCR the same image twice
  (the same image arrives via retweets / fallback feeds);
* **budget gate** — over budget ⇒ skip vision and degrade (fail-safe), rather
  than blowing the LLM cap.

The real extractor (multimodal LLM) is injected; ``FakeVisionExtractor`` is used
in tests. Everything here is import-pure.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from pydantic import BaseModel as _BaseModel

from ..schemas.mention import Mention


class VisionExtractor(Protocol):
    async def extract(self, image_url: str, *, source_post_id: str = "") -> list[Mention]: ...


class Budget(Protocol):
    def allow(self) -> bool: ...


class CountBudget:
    """Allows up to ``limit`` calls, then denies (simple test/local budget)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0

    def allow(self) -> bool:
        if self._used >= self._limit:
            return False
        self._used += 1
        return True


def _media_key(image_url: str) -> str:
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()


class CachingBudgetedVision:
    """Wraps a :class:`VisionExtractor` with a media-URL cache + budget gate."""

    def __init__(
        self,
        inner: VisionExtractor,
        *,
        budget: Budget | None = None,
        cache: dict[str, list[Mention]] | None = None,
    ) -> None:
        self._inner = inner
        self._budget = budget
        self._cache: dict[str, list[Mention]] = cache if cache is not None else {}

    async def extract(self, image_url: str, *, source_post_id: str = "") -> list[Mention]:
        key = _media_key(image_url)
        if key in self._cache:
            return self._cache[key]
        if self._budget is not None and not self._budget.allow():
            return []  # over budget -> degrade, skip vision
        result = await self._inner.extract(image_url, source_post_id=source_post_id)
        self._cache[key] = result
        return result


class FakeVisionExtractor:
    """Returns canned mentions per image URL; counts calls (for cache tests)."""

    def __init__(self, mapping: dict[str, list[Mention]]) -> None:
        self._mapping = mapping
        self.calls = 0

    async def extract(self, image_url: str, *, source_post_id: str = "") -> list[Mention]:
        self.calls += 1
        return [m.model_copy(update={"source_post_id": source_post_id}) for m in self._mapping.get(image_url, [])]


# --- M11: the real multimodal extractor --------------------------------------

VISION_SYSTEM_PROMPT = """\
You read finance images from X/Twitter posts: chart screenshots, position/P&L
screenshots, watchlist screenshots. Extract the tickers the image is actually
about.

For each ticker: the symbol shown; the direction the image implies (a chart
with long levels drawn / a long position => long; short setup / put position
=> short; a plain chart or unclear => unclear); any explicit price levels drawn
or listed; and what kind of image it is (chart | position | watchlist | other).
Calibrated confidence in [0,1] — screenshots are noisy, do not guess symbols
you cannot clearly read. No tickers visible => has_tickers=false."""


class VisionItem(_BaseModel):
    symbol: str
    direction: str = "unclear"  # long|short|unclear
    levels: list[float] = []
    kind: str = "chart"  # chart|position|watchlist|other
    confidence: float = 0.5


class VisionRead(_BaseModel):
    has_tickers: bool = False
    items: list[VisionItem] = []


def _to_mentions(read: "VisionRead", *, source_post_id: str, min_confidence: float = 0.6) -> list[Mention]:
    from ..schemas.mention import AssetClass, ResolutionMethod, Stance
    from .symbology import classify_symbol

    stance_map = {"long": Stance.BULLISH, "short": Stance.BEARISH}
    out: list[Mention] = []
    if not read.has_tickers:
        return out
    for item in read.items:
        sym = item.symbol.strip().lstrip("$").upper()
        if not sym or len(sym) > 6 or item.confidence < min_confidence:
            continue
        stance = stance_map.get(item.direction, Stance.NEUTRAL)
        out.append(Mention(
            symbol=sym,
            asset_class=AssetClass(classify_symbol(sym, "")),
            resolution_method=ResolutionMethod.VISION_OCR,
            mention_confidence=round(min(0.9, item.confidence), 2),  # image reads cap below text
            stance=stance,
            stance_confidence=round(item.confidence * 0.85, 2) if stance is not Stance.NEUTRAL else 0.5,
            surface_text=f"image:{item.kind}",
            source_post_id=source_post_id,
        ))
    return out


def build_llm_vision(model: str | None):  # pragma: no cover - prod wiring
    """Multimodal LLM vision extractor via instructor; None when no model."""
    if not model:
        return None

    class _LlmVision:
        async def extract(self, image_url: str, *, source_post_id: str = "") -> list[Mention]:
            import instructor  # lazy: part of the `prod` extra

            client = instructor.from_provider(model, async_client=True)
            read = await client.chat.completions.create(
                response_model=VisionRead,
                max_retries=1,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": VISION_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        instructor.Image.from_url(image_url),
                        "Extract the tickers from this image.",
                    ]},
                ],
            )
            return _to_mentions(read, source_post_id=source_post_id)

    return _LlmVision()
