"""LLM relevance gate (post-RDDT-incident).

Cashtags are sometimes not about the asset: "$RDDT" meaning Reddit-the-website
("my thesis got called BS on $RDDT"), tickers as community/app names, memes,
usernames. Rules can't disambiguate that — this gate asks the LLM, in ONE call
per post, which extracted symbols are actually being discussed as tradeable
assets, and the pipeline drops confident non-asset references.

Fail-open by design: no model configured -> no gate; any LLM failure -> keep
every mention (an LLM outage must never silence the feed, matching the stance
classifier's degraded-mode posture).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

RELEVANCE_SYSTEM_PROMPT = """\
You verify ticker symbols extracted from one X/Twitter post. For EACH symbol,
decide whether the post is actually discussing it as a TRADEABLE ASSET (stock,
crypto, ETF, option underlying).

Not an asset reference (is_asset_reference=false):
- The cashtag names a website/app/community, not the security: "my thesis got
  called BS on $RDDT" = Reddit the site; "found it on $GOOGL" = Google search.
- Usernames, memes or jokes where the symbol isn't about the asset's value.

IS an asset reference (is_asset_reference=true):
- Price/momentum/position talk, charts, fundamentals, catalysts, comparisons
  ("$RDDT $HOOD $NFLX all green"), even sarcastic takes about the asset.

If genuinely ambiguous, return is_asset_reference=true with low confidence —
the system would rather keep a borderline mention than silence a real one.
Return calibrated confidence in [0,1] for every symbol you were given."""


class TickerRelevance(BaseModel):
    symbol: str
    is_asset_reference: bool = True
    confidence: float = Field(default=0.5, ge=0, le=1)
    reason: str = ""


class RelevanceVerdict(BaseModel):
    tickers: list[TickerRelevance] = Field(default_factory=list)


class RelevanceGate(Protocol):
    async def check(self, text: str, symbols: list[str]) -> dict[str, TickerRelevance]: ...


class LlmRelevanceGate:
    """One LLM call per post covering all extracted symbols; returns verdicts
    keyed by upper-cased symbol. Symbols the model omits are simply not gated."""

    def __init__(self, *, infer: Callable[[str, list[str]], Awaitable[RelevanceVerdict]]) -> None:
        self._infer = infer

    async def check(self, text: str, symbols: list[str]) -> dict[str, TickerRelevance]:
        verdict = await self._infer(text, symbols)
        return {t.symbol.upper().lstrip("$"): t for t in verdict.tickers}


def build_llm_relevance_infer(
    model: str, budget=None,
) -> Callable[[str, list[str]], Awaitable[RelevanceVerdict]]:  # pragma: no cover
    """Wire the async LLM inference via instructor (same plumbing as stance)."""

    async def infer(text: str, symbols: list[str]) -> RelevanceVerdict:
        import instructor  # lazy: part of the `prod` extra

        from ..ops.llm_budget import consume_or_raise

        consume_or_raise(budget)  # over budget -> raise -> pipeline keeps mentions
        client = instructor.from_provider(model, async_client=True)
        return await client.chat.completions.create(
            response_model=RelevanceVerdict,
            max_retries=2,
            max_tokens=1024,  # per-symbol verdicts; Anthropic requires max_tokens
            messages=[
                {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Symbols: {', '.join('$' + s for s in symbols)}\n\nPost:\n{text}",
                },
            ],
        )

    return infer


def build_relevance_gate(*, model: str | None = None, budget=None) -> RelevanceGate | None:
    """LLM gate when a model is configured; None (no gating) otherwise."""
    if not model:
        return None
    return LlmRelevanceGate(infer=build_llm_relevance_infer(model, budget))
