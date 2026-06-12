"""ORM models — M0 subset of the full plan ERD.

Schema-compatible with the full design (docs/plans). M0 needs:
accounts, posts, ticker_mentions, sent_messages, audit_log.

Load-bearing constraints for the M0 invariants:
- ``posts`` unique (account_id, platform_post_id) -> ingest dedupe (INV-4).
- ``sent_messages`` unique idempotency_key -> claim-before-send (INV-1/INV-4).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    """A watched X/Twitter account. Keyed on the stable numeric platform id,
    never the handle (handles can change)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    handle: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(8), default="COLD")  # HOT|WARM|COLD
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    posts: Mapped[list["Post"]] = relationship(back_populates="account")


class Post(Base):
    """An ingested post. (account_id, platform_post_id) is the dedupe key."""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "platform_post_id", name="uq_post_account_platform"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    platform_post_id: Mapped[str] = mapped_column(String(64))
    post_type: Mapped[str] = mapped_column(String(16), default="original")
    text: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    state: Mapped[str] = mapped_column(String(16), default="seen")
    # M1: cross-provider near-dup detection + tombstones.
    content_sha: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped["Account"] = relationship(back_populates="posts")
    mentions: Mapped[list["TickerMention"]] = relationship(back_populates="post")


class TickerMention(Base):
    """A ticker reference extracted from a post (M0: cashtags only)."""

    __tablename__ = "ticker_mentions"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_class: Mapped[str] = mapped_column(String(12), default="equity")
    resolution_method: Mapped[str] = mapped_column(String(24), default="cashtag_exact")
    mention_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # M1: stance is a first-class field with its own confidence.
    stance: Mapped[str] = mapped_column(String(8), default="neutral")
    negation_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    stance_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    option_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    post: Mapped["Post"] = relationship(back_populates="mentions")


class SentMessage(Base):
    """Idempotency ledger for outbound Telegram messages.

    The unique ``idempotency_key`` is the authority: a row is claimed (INSERT)
    BEFORE the Telegram send, so a worker restart or duplicate event can never
    double-post (INV-1, INV-4).
    """

    __tablename__ = "sent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|sent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditLog(Base):
    """Append-only event log."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MentionOutcome(Base):
    """M9: what the market did after a mention — the event-study row.

    Forward returns are computed from split-adjusted daily closes, anchored at
    the first close on/after the post date, alongside the benchmark (SPY) over
    the same windows. Rows are created as soon as an anchor close exists and
    re-completed nightly until fwd_5d fills in.
    """

    __tablename__ = "mention_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    mention_id: Mapped[int] = mapped_column(ForeignKey("ticker_mentions.id"), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    stance: Mapped[str] = mapped_column(String(8), default="neutral")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    px_post: Mapped[float] = mapped_column(Float)
    fwd_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_3d: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_3d: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ExplicitCall(Base):
    """M9-C: a trade the account STATED, recorded verbatim and scored on its
    own terms — the public accountability ledger for fintwit calls."""

    __tablename__ = "explicit_calls"
    __table_args__ = (UniqueConstraint("post_id", "symbol", name="uq_call_post_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    direction: Mapped[str] = mapped_column(String(5))  # long|short
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)   # None = at-market (anchor close)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets: Mapped[list] = mapped_column(JSON, default=list)
    horizon_raw: Mapped[str | None] = mapped_column(String(48), nullable=True)
    horizon_days: Mapped[int] = mapped_column(default=10)  # trading days to timeout
    is_option: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    stated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    close_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)  # stop|target|timeout
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)    # only when a stop was stated
    realized_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # direction-signed move
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PostConviction(Base):
    """M15: the author's commitment level in this post — 0 = musing/watching,
    1 = stated position with size and levels. Weighs sentiment + routes alerts."""

    __tablename__ = "post_conviction"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    conviction: Mapped[float] = mapped_column(Float, default=0.5)
    is_position: Mapped[bool] = mapped_column(Boolean, default=False)


class PostEngagement(Base):
    """At-ingest engagement snapshot — the raw tape for future crowdedness /
    cabal analytics. Captured once per post (~2 min after it's published, so
    it's an early-velocity baseline, not a final count)."""

    __tablename__ = "post_engagement"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    likes: Mapped[int] = mapped_column(default=0)
    retweets: Mapped[int] = mapped_column(default=0)
    replies: Mapped[int] = mapped_column(default=0)
    views: Mapped[int] = mapped_column(default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CallScanCursor(Base):
    """Posts already scanned for explicit calls (drives the rolling backfill)."""

    __tablename__ = "call_scan_cursor"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)


class WatchedTicker(Base):
    """User's per-ticker watchlist: 🔔 on alerts + always in the pulse deep-dive."""

    __tablename__ = "watched_tickers"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- M2: narratives + sentiment -------------------------------------------


class Narrative(Base):
    """A market narrative (theme). Identity is stable; momentum evolves."""

    __tablename__ = "narratives"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    momentum_state: Mapped[str] = mapped_column(String(12), default="rising")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NarrativeMember(Base):
    """Append-only (narrative, instrument, weight) as-of a time (INV-2)."""

    __tablename__ = "narrative_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NarrativeSnapshot(Base):
    """Point-in-time narrative state frozen for reproducibility (INV-2)."""

    __tablename__ = "narrative_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    momentum_state: Mapped[str] = mapped_column(String(12))
    members: Mapped[dict] = mapped_column(JSON, default=dict)


class AccountScore(Base):
    """Point-in-time credibility for an account (closure-time correct, INV-3).

    Populated by the M4 feedback loop. Before any outcomes exist, M2 falls back
    to a tier-based prior.
    """

    __tablename__ = "account_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    sample_size: Mapped[int] = mapped_column(default=0)
    decayed_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# --- M3: instruments + recommendations ------------------------------------


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    asset_class: Mapped[str] = mapped_column(String(12), default="equity")
    name: Mapped[str] = mapped_column(String(128), default="")
    tradeable: Mapped[bool] = mapped_column(Boolean, default=True)
    halted: Mapped[bool] = mapped_column(Boolean, default=False)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_class: Mapped[str] = mapped_column(String(12), default="equity")
    direction: Mapped[str] = mapped_column(String(8))
    entry: Mapped[float] = mapped_column(Float)
    stop: Mapped[float] = mapped_column(Float)
    targets: Mapped[dict] = mapped_column(JSON, default=dict)
    size_hint: Mapped[str] = mapped_column(String(24), default="")
    horizon: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    narrative: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(12), default="candidate")
    suppress_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credibility_at_issuance: Mapped[float] = mapped_column(Float, default=0.0)
    # Attribution contribs [{account, stance(+/-1), conf, mention_time}] for scoring.
    sources: Mapped[list] = mapped_column(JSON, default=list)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RecommendationSource(Base):
    __tablename__ = "recommendation_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    handle: Mapped[str] = mapped_column(String(64), default="")
    attribution_weight: Mapped[float] = mapped_column(Float, default=0.0)


class GateEvaluation(Base):
    __tablename__ = "gate_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    gate_name: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column(Boolean)
    measured: Mapped[dict] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id"), unique=True, index=True
    )
    close_reason: Mapped[str] = mapped_column(String(16))
    realized_r: Mapped[float] = mapped_column(Float)
    mfe_r: Mapped[float] = mapped_column(Float, default=0.0)
    mae_r: Mapped[float] = mapped_column(Float, default=0.0)
    benchmark_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- M5: control plane (kill switch / pause / budget) ----------------------


class SystemFlag(Base):
    """Postgres-authoritative control flags (killswitch, pause mode)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BudgetLedger(Base):
    """Durable spend ledger; ``ref`` is the idempotent charge key."""

    __tablename__ = "budget_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), index=True)  # llm|x_api|market_data
    amount: Mapped[float] = mapped_column(Float)
    ref: Mapped[str] = mapped_column(String(128), unique=True)
    charged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- M7: market data (as-of-issuance unadjusted bars + corporate actions) ---


class MarketBar(Base):
    """Immutable as-of-issuance OHLC bar (unadjusted). The scorer reads these."""

    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", "source", name="uq_bar"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    interval: Mapped[str] = mapped_column(String(8), default="1d")
    ts: Mapped[int] = mapped_column(BigInteger)  # epoch seconds
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    adjustment_basis: Mapped[str] = mapped_column(String(12), default="unadjusted")
    source: Mapped[str] = mapped_column(String(16), default="polygon")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Adjustment(Base):
    """Corporate-action ledger (splits/dividends), replayed forward by the scorer."""

    __tablename__ = "adjustments"
    __table_args__ = (
        UniqueConstraint("symbol", "ex_ts", "kind", "source", name="uq_adjustment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    ex_ts: Mapped[int] = mapped_column(BigInteger)  # epoch seconds of ex-date
    kind: Mapped[str] = mapped_column(String(12))   # split | dividend
    value: Mapped[float] = mapped_column(Float)     # split ratio, or cash/share
    source: Mapped[str] = mapped_column(String(16), default="polygon")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
