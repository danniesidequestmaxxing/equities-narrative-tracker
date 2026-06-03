"""Postgres-authoritative kill switch + pause (M5).

The killswitch and pause state live in Postgres (the authority, INV-1). Reads are
**fail-closed**: if the DB can't be read, ``is_killed`` returns True so the system
suppresses rather than broadcasts blind.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import SystemFlag

log = logging.getLogger(__name__)

PAUSE_NONE = "none"
PAUSE_BROADCAST = "broadcast"  # keep ingest+analysis, hold outbound calls
PAUSE_FULL = "full"            # halt the pipeline


async def set_flag(sf: async_sessionmaker[AsyncSession], key: str, value: str) -> None:
    async with sf() as session:
        existing = await session.scalar(select(SystemFlag).where(SystemFlag.key == key))
        if existing is not None:
            existing.value = value
            existing.updated_at = datetime.now(timezone.utc)
        else:
            session.add(SystemFlag(key=key, value=value))
        await session.commit()


async def get_flag(sf: async_sessionmaker[AsyncSession], key: str, default: str | None = None) -> str | None:
    async with sf() as session:
        value = await session.scalar(select(SystemFlag.value).where(SystemFlag.key == key))
    return value if value is not None else default


async def engage_killswitch(sf) -> None:
    await set_flag(sf, "killswitch", "engaged")


async def disengage_killswitch(sf) -> None:
    await set_flag(sf, "killswitch", "disengaged")


async def is_killed(sf) -> bool:
    """Fail-closed: any read error is treated as 'killed'."""
    try:
        return (await get_flag(sf, "killswitch", "disengaged")) == "engaged"
    except Exception as exc:  # noqa: BLE001 - fail-closed
        log.error("killswitch read failed; failing closed (killed): %s", exc)
        return True


async def set_pause(sf, mode: str) -> None:
    await set_flag(sf, "pause", mode)


async def get_pause(sf) -> str:
    try:
        return await get_flag(sf, "pause", PAUSE_NONE) or PAUSE_NONE
    except Exception as exc:  # noqa: BLE001 - fail-closed -> full pause
        log.error("pause read failed; failing closed (full pause): %s", exc)
        return PAUSE_FULL
