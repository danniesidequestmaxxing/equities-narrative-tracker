"""End-to-end spine: post -> dedupe -> cashtag -> idempotent alert.

These are the M0 exit criteria as executable proofs.
"""

from datetime import datetime, timezone
from typing import Any

from narrative_tracker.db import idempotency, repo
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.worker import process_post


def _notifier(bot, session_factory) -> AlertNotifier:
    return AlertNotifier(bot=bot, session_factory=session_factory, trading_chat_id=100)


async def test_single_post_emits_one_alert(session_factory, fake_bot, make_post):
    notifier = _notifier(fake_bot, session_factory)
    result = await process_post(
        make_post(text="$AAPL looking strong", post_id="p1"),
        session_factory=session_factory,
        notifier=notifier,
    )
    assert result["symbols"] == ["AAPL"]
    assert result["alerts_sent"] == 1
    assert len(fake_bot.sent) == 1
    assert "AAPL" in fake_bot.sent[0]["text"]


async def test_same_post_twice_is_deduped(session_factory, fake_bot, make_post):
    notifier = _notifier(fake_bot, session_factory)
    post = make_post(text="$AAPL", post_id="p1")
    first = await process_post(post, session_factory=session_factory, notifier=notifier)
    second = await process_post(post, session_factory=session_factory, notifier=notifier)
    assert first["alerts_sent"] == 1
    assert second["alerts_sent"] == 0
    assert second["deduped"] is True
    # The decisive M0 guarantee: exactly one message ever sent.
    assert len(fake_bot.sent) == 1


async def test_multiple_cashtags_emit_multiple_alerts(session_factory, fake_bot, make_post):
    notifier = _notifier(fake_bot, session_factory)
    result = await process_post(
        make_post(text="long $NVDA and $AMD; $4200 SPX level", post_id="p2"),
        session_factory=session_factory,
        notifier=notifier,
    )
    assert sorted(result["symbols"]) == ["AMD", "NVDA"]  # $4200 filtered out
    assert result["alerts_sent"] == 2
    assert len(fake_bot.sent) == 2


async def test_crash_between_insert_and_send_recovers(session_factory, fake_bot, make_post):
    """If the worker crashed after the post row was written but before the alert,
    a restart must still send the alert (the claim was never made)."""
    account_id = await repo.get_or_create_account(
        session_factory, platform_user_id="u1", handle="trader"
    )
    await idempotency.insert_post_if_new(
        session_factory,
        account_id=account_id,
        platform_post_id="p3",
        text="$TSLA",
        posted_at=datetime.now(timezone.utc),
    )

    notifier = _notifier(fake_bot, session_factory)
    result = await process_post(
        make_post(text="$TSLA", post_id="p3", user_id="u1"),
        session_factory=session_factory,
        notifier=notifier,
    )
    assert result["deduped"] is True       # post already existed
    assert result["alerts_sent"] == 1      # ...but the alert was never sent -> send it now
    assert len(fake_bot.sent) == 1


async def test_no_cashtags_no_alert(session_factory, fake_bot, make_post):
    notifier = _notifier(fake_bot, session_factory)
    result = await process_post(
        make_post(text="market feels heavy today, no positions", post_id="p4"),
        session_factory=session_factory,
        notifier=notifier,
    )
    assert result["symbols"] == []
    assert result["alerts_sent"] == 0
    assert fake_bot.sent == []


class _FlakyBot:
    """Raises a MarkdownV2 parse error on the first (MarkdownV2) attempt, then
    succeeds on the plain-text retry."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any):
        self.calls.append({"text": text, "kwargs": kwargs})
        if kwargs.get("parse_mode") == "MarkdownV2":
            raise RuntimeError("Bad Request: can't parse entities at byte offset 12")

        class _Result:
            message_id = 1

        return _Result()


async def test_safe_send_falls_back_to_plain(session_factory, make_post):
    bot = _FlakyBot()
    notifier = _notifier(bot, session_factory)
    sent = await notifier.send_alert(
        make_post(text="$BRK.B", post_id="p9", user_id="u9", handle="value_guy"),
        {"symbol": "BRK.B", "asset_class": "equity"},
    )
    assert sent is True
    # First attempt (MarkdownV2) raised; second attempt (plain) succeeded.
    assert len(bot.calls) == 2
    assert bot.calls[0]["kwargs"].get("parse_mode") == "MarkdownV2"
    assert "parse_mode" not in bot.calls[1]["kwargs"]
