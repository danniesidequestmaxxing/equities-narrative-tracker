"""Dead-man's-switch heartbeat.

Ping an external watchdog (Healthchecks.io) **on row-written** — not on loop
iteration — so a silently-frozen feed (the pipeline runs but ingests nothing) is
detected. No-op when no URL is configured, so M0 runs without it.

``httpx`` is imported lazily (part of the ``prod`` extra).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def ping_heartbeat(url: str | None) -> None:
    """Best-effort heartbeat ping. Never raises into the caller."""
    if not url:
        return
    try:
        import httpx  # lazy: part of the `prod` extra

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(url)
    except Exception as exc:  # noqa: BLE001 - heartbeat must never break ingest
        log.warning("heartbeat ping failed: %s", exc)
