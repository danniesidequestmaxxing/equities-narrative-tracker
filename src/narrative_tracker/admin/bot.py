"""aiogram admin-command listener (prod). Thin shell over commands.handle_command."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def run_admin_bot(bot, sf, admin_ids: list[int], *, market=None) -> None:  # pragma: no cover
    from aiogram import Dispatcher
    from aiogram.types import Message

    from .commands import handle_command

    dp = Dispatcher()

    @dp.message()
    async def _on_message(message: Message) -> None:
        # Seamless input: /commands, @handle (track account), $TICKER (brief/watch).
        if not message.text or message.text[0] not in "/@$":
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
