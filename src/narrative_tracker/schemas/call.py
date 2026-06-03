"""TradeCall contract (M3) — shared by the recommender, the notifier, and the
scorer's outcome labeling (M4)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Targets(BaseModel):
    t1: float
    t2: float | None = None


class TradeCall(BaseModel):
    call_id: str
    symbol: str
    asset_class: str
    direction: Direction
    entry: float
    stop: float
    targets: Targets
    size_hint: str
    horizon: str
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""
    source_accounts: list[str] = Field(default_factory=list)
    narrative: str | None = None
    disclaimer: str = "NOT FINANCIAL ADVICE. For information only."

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        reward = abs(self.targets.t1 - self.entry)
        return round(reward / risk, 2) if risk else 0.0
