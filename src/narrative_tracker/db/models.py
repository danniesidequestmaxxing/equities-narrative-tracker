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
