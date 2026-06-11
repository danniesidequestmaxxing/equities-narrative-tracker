"""Daily LLM call budget (M12).

One shared counter across every LLM surface (stance, relevance, call-scan,
vision). When the day's cap is hit, callers raise and their existing fail-open
paths take over — stance degrades to rule-based, relevance keeps mentions,
call-scan retries tomorrow, vision skips. The system gets dumber for a few
hours instead of the bill getting bigger; the cap resets at UTC midnight.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)


class DailyCallBudget:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._day: date | None = None
        self._used = 0
        self._warned = False

    def _roll(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._day != today:
            self._day = today
            self._used = 0
            self._warned = False

    def allow(self) -> bool:
        """Consume one call if the day's budget permits."""
        if self._limit <= 0:
            return True  # 0 = uncapped
        self._roll()
        if self._used >= self._limit:
            if not self._warned:
                log.warning("LLM daily call budget (%d) exhausted — degrading to rule-based until UTC midnight", self._limit)
                self._warned = True
            return False
        self._used += 1
        return True

    def snapshot(self) -> dict:
        self._roll()
        return {"used": self._used, "limit": self._limit}


class BudgetExhausted(RuntimeError):
    """Raised by LLM callers when the daily budget denies a call."""


def consume_or_raise(budget: "DailyCallBudget | None") -> None:
    if budget is not None and not budget.allow():
        raise BudgetExhausted("LLM daily call budget exhausted")
