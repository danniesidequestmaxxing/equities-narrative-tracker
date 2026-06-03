"""Telegram alert notifier (M0).

Sends one idempotent alert per (post, ticker). The flow is **claim-before-send**
(INV-1): claim the unique idempotency key in Postgres, then send, then record the
``message_id``. A worker restart or duplicate event can never double-post.

``safe_send`` guarantees a MarkdownV2 parse bug degrades to a plain-text send
rather than a dropped alert.

The bot is injected (the real ``aiogram`` ``Bot`` in prod, a fake in tests), so
this module imports cleanly without the ``prod`` extra.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import idempotency
from ..ingest.provider import RawPost
from ..schemas.mention import Mention, OptionDetail, Stance
from .escaping import md, md_code, md_url

log = logging.getLogger(__name__)

_STANCE_EMOJI = {Stance.BULLISH: "\U0001f7e2", Stance.BEARISH: "\U0001f534"}  # 🟢 🔴


class BotProtocol(Protocol):
    """Minimal surface we need from a Telegram bot (aiogram-compatible)."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...


def tradingview_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={symbol}"


def _option_str(od: OptionDetail) -> str:
    if od.strike is not None:
        strike = int(od.strike) if od.strike == int(od.strike) else od.strike
        core = f"{strike}{od.right.value}"
    else:
        core = od.right.value
    return f"{core} {od.expiry_raw}" if od.expiry_raw else core


def build_alert(post: RawPost, mention: Mention) -> tuple[str, str]:
    """Return ``(markdown_v2, plain)`` for a single ticker alert."""
    symbol = mention.symbol
    tv = tradingview_url(symbol)
    handle_url = f"https://x.com/{post.handle}" if post.handle else tv
    asset = mention.asset_class.value
    emoji = _STANCE_EMOJI.get(mention.stance, "\U0001f7e1")  # 🟡 default
    opt = f" {_option_str(mention.option_detail)}" if mention.option_detail else ""
    header = f"${symbol}{opt}"
    mdv2 = (
        f"⚡ *{md(header)}* · {emoji} {md(mention.stance.value)} · {md(asset)}\n"
        f"[{md('@' + (post.handle or 'source'))}]({md_url(handle_url)}) "
        f"posted on `{md_code('$' + symbol)}`\n"
        f"{md(post.posted_at.strftime('%H:%M ET'))}\n"
        f"[\U0001f4c8 Chart]({md_url(tv)}) · {md('#' + symbol)}\n"
        f"\n"
        f"_Derived signal · not financial advice_"
    )
    plain = (
        f"[ALERT] ${symbol}{opt} {mention.stance.value} ({asset})\n"
        f"@{post.handle or 'source'} posted on ${symbol} at "
        f"{post.posted_at.strftime('%H:%M ET')}\n"
        f"{tv}\n"
        f"Derived signal - not financial advice"
    )
    return mdv2, plain


class AlertNotifier:
    """Builds and sends idempotent ticker alerts to the trading channel."""

    def __init__(
        self,
        *,
        bot: BotProtocol,
        session_factory: async_sessionmaker[AsyncSession],
        trading_chat_id: int,
    ) -> None:
        self._bot = bot
        self._sf = session_factory
        self._chat_id = trading_chat_id

    @staticmethod
    def _mention_key(post: RawPost, mention: Mention) -> str:
        key = idempotency.alert_idempotency_key(
            post.platform_user_id, post.platform_post_id, mention.symbol
        )
        if mention.option_detail:
            od = mention.option_detail
            key += f":{od.right.value}{od.strike}:{od.expiry_raw or ''}"
        return key

    async def send_alert(self, post: RawPost, mention: Mention) -> bool:
        """Send one alert for a (post, ticker). Returns ``True`` if a message was
        actually sent, ``False`` if it was a deduped no-op."""
        key = self._mention_key(post, mention)
        # Claim BEFORE sending (INV-1): if already claimed, do not send.
        claimed = await idempotency.claim_send(
            self._sf, idempotency_key=key, chat_id=self._chat_id
        )
        if not claimed:
            log.debug("alert already claimed, skipping: %s", key)
            return False

        mdv2, plain = build_alert(post, mention)
        message_id = await self._safe_send(mdv2, plain)
        await idempotency.mark_sent(
            self._sf, idempotency_key=key, telegram_message_id=message_id
        )
        return True

    async def _safe_send(self, text_mdv2: str, plain_fallback: str) -> int:
        """Send MarkdownV2; on a parse error, resend as plain text so a template
        bug never drops an alert. Returns the Telegram ``message_id``."""
        try:
            result = await self._bot.send_message(
                self._chat_id, text_mdv2, parse_mode="MarkdownV2"
            )
        except Exception as exc:  # noqa: BLE001
            if "can't parse entities" in str(exc).lower():
                log.error("MarkdownV2 parse failure; sending plain text")
                result = await self._bot.send_message(self._chat_id, plain_fallback)
            else:
                raise
        return int(getattr(result, "message_id", 0) or 0)


def build_aiogram_bot(token: str) -> BotProtocol:
    """Construct a real aiogram Bot (lazy import; part of the ``prod`` extra)."""
    try:
        from aiogram import Bot
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError("aiogram not installed; `pip install -e '.[prod]'`") from exc
    return Bot(token=token)  # type: ignore[return-value]
