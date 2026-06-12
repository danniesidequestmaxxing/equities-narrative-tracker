"""The Mention contract.

Two confidences are kept **separate and orthogonal** (see
docs/design/04-extraction-cascade.md):

* ``mention_confidence`` — "is this really a reference to symbol X?"
* ``stance_confidence``  — "given it's X, is the direction right?"

An inverted stance is a wrong trade even when the symbol is certain, so the two
must be independently gateable downstream (the plan's θ/φ gates).
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    OPTION = "option"
    CRYPTO = "crypto"
    INDEX = "index"
    FX = "fx"
    UNKNOWN = "unknown"


class ResolutionMethod(str, Enum):
    CASHTAG_EXACT = "cashtag_exact"
    CASHTAG_DISAMBIG = "cashtag_disambiguated"
    NER_ENTITY_LINK = "ner_entity_link"
    LLM_INFERENCE = "llm_inference"
    VISION_OCR = "vision_ocr"
    ALIAS_TABLE = "alias_table"
    REJECTED = "rejected"


class Stance(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class OptionDetail(BaseModel):
    right: OptionRight
    strike: float | None = None
    expiry: date | None = None
    expiry_raw: str | None = None  # the token as written ("0DTE", "Jan'27")
    is_leaps: bool = False
    dte_relative: int | None = None


class Mention(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.UNKNOWN
    resolution_method: ResolutionMethod
    stance: Stance = Stance.NEUTRAL
    negation_flag: bool = False
    mention_confidence: float = Field(default=1.0, ge=0, le=1)
    # M15: author's commitment level (post-level; not persisted on the mention row)
    conviction: float = Field(default=0.5, ge=0, le=1)
    is_position: bool = False
    stance_confidence: float = Field(default=0.0, ge=0, le=1)
    option_detail: OptionDetail | None = None

    # Provenance — required for audit and credibility attribution.
    surface_text: str = ""
    source_post_id: str = ""
    is_quoted_signal: bool = False
    thread_root_id: str | None = None
    rationale: str | None = None

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, v: str) -> str:
        return v.strip().lstrip("$").upper()

    def to_row(self) -> dict:
        """Flatten to the dict shape persisted by ``db.repo.add_mentions``."""
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "resolution_method": self.resolution_method.value,
            "mention_confidence": self.mention_confidence,
            "stance": self.stance.value,
            "negation_flag": self.negation_flag,
            "stance_confidence": self.stance_confidence,
            "option_detail": (
                self.option_detail.model_dump(mode="json")
                if self.option_detail
                else None
            ),
        }
