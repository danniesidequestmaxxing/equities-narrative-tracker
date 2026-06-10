"""Live admin commands (/addsource etc.) + the dynamic watchlist."""

from narrative_tracker.admin.commands import handle_command
from narrative_tracker.db import repo

ADMIN = [999]


async def test_addsource_adds_to_dynamic_watchlist(session_factory):
    r = await handle_command("/addsource @whale tier=HOT", 999, session_factory, ADMIN)
    assert "Watching @whale" in r and "HOT" in r
    assert "whale" in await repo.active_handles(session_factory)  # poller will see it
    listed = await handle_command("/sources", 999, session_factory, ADMIN)
    assert "@whale" in listed and "HOT" in listed


async def test_non_admin_is_rejected(session_factory):
    r = await handle_command("/addsource @x", 123, session_factory, ADMIN)
    assert "Not authorized" in r
    assert await repo.active_handles(session_factory) == []  # nothing happened


async def test_no_admins_configured_locks_everything(session_factory):
    assert "Not authorized" in await handle_command("/addsource @x", 999, session_factory, [])


async def test_rmsource_removes_from_watchlist(session_factory):
    await handle_command("/addsource @whale", 999, session_factory, ADMIN)
    r = await handle_command("/rmsource @whale", 999, session_factory, ADMIN)
    assert "Stopped watching" in r
    assert await repo.active_handles(session_factory) == []


async def test_tier_and_status(session_factory):
    await handle_command("/addsource @whale tier=COLD", 999, session_factory, ADMIN)
    assert "WARM" in await handle_command("/tier @whale WARM", 999, session_factory, ADMIN)
    assert "watching 1" in await handle_command("/status", 999, session_factory, ADMIN)
