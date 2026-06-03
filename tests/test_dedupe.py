"""Dedupe taxonomy, edit detection, cross-provider near-dup, tombstone (M1)."""

from datetime import datetime, timezone

from narrative_tracker.db import idempotency, repo
from narrative_tracker.ingest import dedupe


def test_classify_post_type():
    assert dedupe.classify_post_type(is_retweet=True) == "retweet"
    assert dedupe.classify_post_type(is_quote=True) == "quote"
    assert dedupe.classify_post_type(is_reply=True) == "reply"
    assert dedupe.classify_post_type() == "original"


def test_is_edited():
    assert dedupe.is_edited(["1", "2"]) is True
    assert dedupe.is_edited(["1"]) is False
    assert dedupe.is_edited(None) is False


async def test_near_duplicate_cross_provider(session_factory):
    account_id = await repo.get_or_create_account(
        session_factory, platform_user_id="u1", handle="trader"
    )
    t = datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc)
    await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id="provA-1",
        text="$NVDA breaking out!",
        posted_at=t,
    )
    # Same author + same content via a different provider id, within the window.
    assert await dedupe.is_near_duplicate(
        session_factory, account_id=account_id, text="$NVDA breaking out!!!", posted_at=t
    ) is True
    # Different content -> not a duplicate.
    assert await dedupe.is_near_duplicate(
        session_factory, account_id=account_id, text="totally unrelated", posted_at=t
    ) is False


async def test_tombstone_marks_deleted(session_factory):
    account_id = await repo.get_or_create_account(
        session_factory, platform_user_id="u1", handle="trader"
    )
    await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id="p9",
        text="$X",
        posted_at=datetime.now(timezone.utc),
    )
    assert await dedupe.tombstone_post(
        session_factory, account_id=account_id, platform_post_id="p9"
    ) is True
    # Unknown post -> nothing updated.
    assert await dedupe.tombstone_post(
        session_factory, account_id=account_id, platform_post_id="missing"
    ) is False
