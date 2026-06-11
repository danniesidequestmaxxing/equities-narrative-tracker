"""Merge multiple source providers into one stream (M13: X + Telegram)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

from .provider import RawPost, SourceProvider

log = logging.getLogger(__name__)


class CompositeProvider:
    """Fans every inner provider's stream into a single queue. One provider
    crashing is logged and dropped; the others keep flowing."""

    def __init__(self, providers: list[SourceProvider]) -> None:
        self._providers = providers

    async def stream(self) -> AsyncIterator[RawPost]:
        queue: asyncio.Queue[RawPost] = asyncio.Queue()

        async def pump(provider: SourceProvider) -> None:
            try:
                async for post in provider.stream():
                    await queue.put(post)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("provider %s died; continuing with the rest", type(provider).__name__)

        tasks = [asyncio.create_task(pump(p)) for p in self._providers]
        try:
            while True:
                yield await queue.get()
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
