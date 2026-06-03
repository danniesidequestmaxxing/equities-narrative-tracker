"""Ingest buffer.

Decouples the stream consumer (which must never block, or it drops the
WebSocket) from the processing loop. M0 uses a bounded in-memory queue; the
durable backing (Redis Stream / Postgres work table per INV-6) lands in M5.

Overflow policy for M0: log loudly and apply backpressure by awaiting a slot
(the stream consumer is the only producer, so a brief await is safe and far
better than silently dropping a post).
"""

from __future__ import annotations

import asyncio
import logging

from .provider import RawPost

log = logging.getLogger(__name__)


class IngestBuffer:
    """A bounded async queue of :class:`RawPost`."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._q: asyncio.Queue[RawPost] = asyncio.Queue(maxsize=maxsize)

    async def put(self, post: RawPost) -> None:
        if self._q.full():
            log.warning(
                "ingest buffer full (%d); applying backpressure", self._q.maxsize
            )
        await self._q.put(post)

    async def get(self) -> RawPost:
        return await self._q.get()

    def task_done(self) -> None:
        self._q.task_done()

    def qsize(self) -> int:
        return self._q.qsize()

    @property
    def maxsize(self) -> int:
        return self._q.maxsize
