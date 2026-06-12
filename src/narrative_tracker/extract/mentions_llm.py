"""LLM mention inference (M14): catch ticker talk with no cashtag.

"Micron is going to rip after earnings", "loaded NVDA here", "the 600c on
spy printed" — clearly about tradeable assets, invisible to the deterministic
extractors. This stage runs ONLY when options/cashtags/aliases found nothing,
and asks the model which tradeable tickers the post is actually about, with
relevance baked into the prompt (so no second gate call is needed).

Honesty controls: inferred mentions carry resolution_method=llm_inference,
their confidence is capped below clean cashtags, alerts tag them as inferred,
and the whole stage is fail-open + daily-budget-gated like every LLM surface.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from ..schemas.mention import AssetClass, Mention, ResolutionMethod, Stance

log = logging.getLogger(__name__)

INFER_SYSTEM_PROMPT = """\
You read one post from a finance/trading account that contains NO cashtags.
Decide which TRADEABLE tickers the post is actually about, if any.

Map confidently:
- company names -> their ticker ("Micron" -> MU, "Coreweave" -> CRWV)
- bare tickers written without $ ("NVDA breaking out" -> NVDA)
- unambiguous products/people -> the issuer ("Ozempic" -> NVO)
- major crypto by name ("bitcoin" -> BTC, asset_class crypto)

Return NOTHING for:
- sector/group talk with no specific name ("semis are running", "the Mag7")
- macro commentary (CPI, the Fed, yields) with no instrument
- the company as a product/website rather than an asset ("found it on Google")
- jokes, greetings, anything not about markets

Only liquid, real symbols you are sure of — a wrong ticker is worse than a
miss. Calibrated confidence per item; when torn, use low confidence."""

_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")


class InferredTicker(BaseModel):
    symbol: str
    asset_class: str = "equity"  # equity|crypto
    confidence: float = Field(default=0.5, ge=0, le=1)


class InferredMentions(BaseModel):
    has_tickers: bool = False
    items: list[InferredTicker] = Field(default_factory=list)


MentionInferrer = Callable[[str], Awaitable[InferredMentions]]


def to_mentions(
    read: InferredMentions, *, source_post_id: str = "", min_confidence: float = 0.65
) -> list[Mention]:
    out: list[Mention] = []
    if not read.has_tickers:
        return out
    for item in read.items:
        sym = item.symbol.strip().lstrip("$").upper()
        if not _SYMBOL_RE.match(sym) or item.confidence < min_confidence:
            continue
        asset = item.asset_class if item.asset_class in ("equity", "crypto") else "equity"
        out.append(Mention(
            symbol=sym,
            asset_class=AssetClass(asset),
            resolution_method=ResolutionMethod.LLM_INFERENCE,
            mention_confidence=round(min(0.85, item.confidence), 2),  # below clean cashtags
            stance=Stance.NEUTRAL,  # S3 stance pass fills this in
            source_post_id=source_post_id,
            surface_text="(inferred)",
        ))
    return out


def build_mention_inferrer(model: str | None, budget=None) -> MentionInferrer | None:  # pragma: no cover
    """LLM inferrer via instructor; None when no model configured."""
    if not model:
        return None

    async def infer(text: str) -> InferredMentions:
        import instructor  # lazy: part of the `prod` extra

        from ..ops.llm_budget import consume_or_raise

        consume_or_raise(budget)  # over budget -> raise -> stage skipped
        client = instructor.from_provider(model, async_client=True)
        return await client.chat.completions.create(
            response_model=InferredMentions,
            max_retries=1,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": INFER_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )

    return infer
