"""Explicit-call extraction (M9-C): record what accounts actually called.

An LLM pass that recognizes STATED trades — "long $X at 12.40, stop 11.80",
"bought $Y here, target 50", "$SPY 600c" — and returns them structured, so the
ledger can score each account's calls on their own terms. Strict by design:
hypotheticals, questions, other people's trades, and recaps of already-closed
trades are NOT calls. Gated on the LLM being configured; fail-open (no calls
extracted on any error — the scan retries the post next cycle).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

CALLS_SYSTEM_PROMPT = """\
You extract EXPLICIT trade calls from one X/Twitter post by a finance account.

A call exists ONLY when the AUTHOR states their own position or actionable
plan: "long $X", "bought $X at 12.40", "short $Y, stop 21", "$SPY 600c",
"adding $Z under 30, target 45". Extract the levels they state — never invent
numbers they didn't write.

NOT a call (return has_call=false):
- hypotheticals/conditionals ("if $X reclaims 50 it's a long"), questions
- commentary or price observations without a stated position
- someone ELSE'S trade being quoted or mocked
- recaps of trades already closed ("sold my $X from last week, +40%")

Direction: long or short. For options: calls => long, puts => short, and set
is_option=true. horizon = their stated timeframe verbatim if any ("swing",
"by Friday", "2026 leaps"). Confidence calibrated in [0,1] — when unsure
whether it is a real stated call, use a LOW confidence rather than guessing."""


class StatedCall(BaseModel):
    symbol: str
    direction: Literal["long", "short"]
    entry: float | None = None
    stop: float | None = None
    targets: list[float] = Field(default_factory=list)
    horizon: str | None = None
    is_option: bool = False
    confidence: float = Field(default=0.5, ge=0, le=1)


class CallExtraction(BaseModel):
    has_call: bool = False
    calls: list[StatedCall] = Field(default_factory=list)


CallExtractor = Callable[[str], Awaitable[CallExtraction]]


def horizon_days(raw: str | None) -> int:
    """Stated timeframe -> trading-day timeout for scoring (default 10)."""
    low = (raw or "").lower()
    if any(k in low for k in ("0dte", "today", "intraday", "tomorrow", "day trade")):
        return 2
    if any(k in low for k in ("week", "friday", "swing", "short term")):
        return 10
    if any(k in low for k in ("month", "earnings")):
        return 21
    if any(k in low for k in ("leap", "long term", "year", "2026", "2027")):
        return 60
    return 10


def build_call_extractor(model: str | None, budget=None) -> CallExtractor | None:  # pragma: no cover
    """LLM extractor via instructor (same plumbing as stance/relevance)."""
    if not model:
        return None

    async def extract(text: str) -> CallExtraction:
        import instructor  # lazy: part of the `prod` extra

        from ..ops.llm_budget import consume_or_raise

        consume_or_raise(budget)  # over budget -> raise -> post retried tomorrow
        client = instructor.from_provider(model, async_client=True)
        return await client.chat.completions.create(
            response_model=CallExtraction,
            max_retries=2,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": CALLS_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )

    return extract
