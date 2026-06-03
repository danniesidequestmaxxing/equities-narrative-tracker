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
