"""Admin service layer (M5).

Source management + control. Every mutation is written to the immutable audit log.
Removing a source is **forward-only** by default (stops future ingestion; keeps
history + credibility) — retroactive deletion would be a separate, explicit action.
The aiogram commands and the FastAPI ``/admin`` endpoints both call into here, so
the two control planes can't drift.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import repo
from ..db.models import Account
from ..ops import killswitch


async def add_source(sf, *, platform_user_id: str, handle: str, tier: str = "COLD") -> int:
    account_id = await repo.get_or_create_account(
        sf, platform_user_id=platform_user_id, handle=handle, tier=tier
    )
    await repo.record_audit(sf, event_type="admin.add_source", payload={"platform_user_id": platform_user_id, "handle": handle, "tier": tier})
    return account_id


async def remove_source(sf, *, platform_user_id: str) -> bool:
    async with sf() as session:
        result = await session.execute(
            update(Account).where(Account.platform_user_id == platform_user_id).values(active=False)
        )
        await session.commit()
    await repo.record_audit(sf, event_type="admin.remove_source", payload={"platform_user_id": platform_user_id})
    return result.rowcount > 0


async def set_tier(sf, *, platform_user_id: str, tier: str) -> bool:
    async with sf() as session:
        result = await session.execute(
            update(Account).where(Account.platform_user_id == platform_user_id).values(tier=tier)
        )
        await session.commit()
    await repo.record_audit(sf, event_type="admin.set_tier", payload={"platform_user_id": platform_user_id, "tier": tier})
    return result.rowcount > 0


async def list_sources(sf) -> list[dict]:
    async with sf() as session:
        rows = (await session.execute(select(Account).order_by(Account.id))).scalars().all()
    return [{"platform_user_id": a.platform_user_id, "handle": a.handle, "tier": a.tier, "active": a.active} for a in rows]


async def pause(sf, mode: str) -> None:
    await killswitch.set_pause(sf, mode)
    await repo.record_audit(sf, event_type="admin.pause", payload={"mode": mode})


async def resume(sf) -> None:
    await killswitch.set_pause(sf, killswitch.PAUSE_NONE)
    await repo.record_audit(sf, event_type="admin.resume", payload={})


async def kill(sf) -> None:
    await killswitch.engage_killswitch(sf)
    await repo.record_audit(sf, event_type="admin.kill", payload={})


async def unkill(sf) -> None:
    await killswitch.disengage_killswitch(sf)
    await repo.record_audit(sf, event_type="admin.unkill", payload={})
