"""aiogram admin-command listener (prod). Thin shell over commands.handle_command."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def run_admin_bot(
    bot, sf, admin_ids: list[int], *, market=None, on_channel_post=None
) -> None:  # pragma: no cover
    from aiogram import Dispatcher
    from aiogram.types import Message

    from .commands import handle_command

    dp = Dispatcher()

    if on_channel_post is not None:
        from datetime import timezone as _tz

        from ..ingest.provider import RawPost

        @dp.channel_post()
        async def _on_channel_post(message: Message) -> None:
            # M13 path 1: channels where this bot is an admin feed straight
            # into the pipeline (text + captions; media needs path 2's public
            # CDN urls, bot file links would embed the token).
            text = message.text or message.caption or ""
            if not text:
                return
            name = (message.chat.username or str(message.chat.id)).lower()
            posted = message.date.astimezone(_tz.utc) if message.date else None
            if posted is None:
                return
            await on_channel_post(RawPost(
                platform_user_id=f"tg:{name}",
                handle=f"tg:{name}",
                platform_post_id=str(message.message_id),
                text=text,
                posted_at=posted,
            ))

    @dp.message()
    async def _on_message(message: Message) -> None:
        # Seamless input: /commands, @handle (track account), $TICKER
        # (brief/watch), t.me/... (track a Telegram channel).
        text = (message.text or "").strip()
        if not text or (text[0] not in "/@$" and not text.lower().startswith(("t.me/", "https://t.me/", "http://t.me/"))):
            return
        from_id = message.from_user.id if message.from_user else 0

        # /export sends a document, not text — handled in the shell.
        if message.text.strip().lower().startswith("/export"):
            if not admin_ids or from_id not in admin_ids:
                await message.answer("⛔ Not authorized.")
                return
            from aiogram.types import BufferedInputFile

            from .export import make_export_zip

            name, data = await make_export_zip(sf)
            await message.answer_document(
                BufferedInputFile(data, filename=name),
                caption="Ledger export: accounts, mention outcomes, stated calls.",
            )
            return

        reply = await handle_command(message.text, from_id, sf, admin_ids, market=market)
        await message.answer(reply)

    log.info("admin command listener started (%d admin id(s))", len(admin_ids))
    await dp.start_polling(bot, handle_signals=False)
