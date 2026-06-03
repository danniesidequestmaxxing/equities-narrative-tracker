"""Idempotency ledger + post dedupe (INV-1, INV-4)."""

from datetime import datetime, timezone

from narrative_tracker.db import idempotency, repo
from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all


async def test_claim_send_dedupes(session_factory):
    key = "ALERT:u1:p1:AAPL"
    assert await idempotency.claim_send(session_factory, idempotency_key=key, chat_id=1) is True
    # Second claim of the same key is refused -> caller will not send.
    assert await idempotency.claim_send(session_factory, idempotency_key=key, chat_id=1) is False


async def test_claim_survives_restart(db_url):
    key = "ALERT:u1:p1:AAPL"

    # "Process 1"
    e1 = build_engine(db_url)
    await create_all(e1)
    sf1 = build_sessionmaker(e1)
    assert await idempotency.claim_send(sf1, idempotency_key=key, chat_id=1) is True
    await e1.dispose()

    # "Process 2" — a fresh engine on the same database (worker restart).
    e2 = build_engine(db_url)
    sf2 = build_sessionmaker(e2)
    assert await idempotency.claim_send(sf2, idempotency_key=key, chat_id=1) is False
    await e2.dispose()


async def test_mark_sent_records_message_id(session_factory):
    from sqlalchemy import select

    from narrative_tracker.db.models import SentMessage

    key = "ALERT:u1:p2:NVDA"
    await idempotency.claim_send(session_factory, idempotency_key=key, chat_id=7)
    await idempotency.mark_sent(session_factory, idempotency_key=key, telegram_message_id=4242)

    async with session_factory() as session:
        row = await session.scalar(
            select(SentMessage).where(SentMessage.idempotency_key == key)
        )
    assert row.status == "sent"
    assert row.telegram_message_id == 4242
    assert row.sent_at is not None


async def test_insert_post_if_new_dedupes(session_factory):
    account_id = await repo.get_or_create_account(
        session_factory, platform_user_id="u1", handle="trader"
    )
    pid1, new1 = await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id="p1",
        text="$AAPL",
        posted_at=datetime.now(timezone.utc),
    )
    pid2, new2 = await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id="p1",
        text="$AAPL",
        posted_at=datetime.now(timezone.utc),
    )
    assert new1 is True and new2 is False
    assert pid1 == pid2 and pid1 is not None
