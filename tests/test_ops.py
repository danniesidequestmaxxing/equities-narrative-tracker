"""M5: kill switch, budget, degraded posture, recovery, admin."""

from datetime import datetime, timedelta, timezone

from narrative_tracker.admin import service
from narrative_tracker.ops import budget, killswitch, recovery
from narrative_tracker.ops.degraded import DependencyHealth, posture


# --- kill switch / pause ---------------------------------------------------


async def test_killswitch_roundtrip(session_factory):
    assert await killswitch.is_killed(session_factory) is False
    await killswitch.engage_killswitch(session_factory)
    assert await killswitch.is_killed(session_factory) is True
    await killswitch.disengage_killswitch(session_factory)
    assert await killswitch.is_killed(session_factory) is False


async def test_pause_modes(session_factory):
    assert await killswitch.get_pause(session_factory) == killswitch.PAUSE_NONE
    await killswitch.set_pause(session_factory, killswitch.PAUSE_BROADCAST)
    assert await killswitch.get_pause(session_factory) == "broadcast"


# --- budget ----------------------------------------------------------------


async def test_budget_idempotent_and_cap(session_factory):
    assert await budget.charge(session_factory, bucket="llm", amount=40.0, ref="r1", cap=100.0) is True
    # Re-charging the same ref must not double-count.
    await budget.charge(session_factory, bucket="llm", amount=40.0, ref="r1", cap=100.0)
    assert await budget.spent(session_factory, "llm") == 40.0
    await budget.charge(session_factory, bucket="llm", amount=70.0, ref="r2", cap=100.0)
    assert await budget.over_budget(session_factory, bucket="llm", cap=100.0) is True


# --- degraded posture (fail-closed) ----------------------------------------


def test_posture_healthy():
    p = posture(DependencyHealth())
    assert p.can_ingest and p.can_alert and p.can_broadcast


def test_posture_postgres_down_suppresses_everything():
    p = posture(DependencyHealth(postgres=False))
    assert not p.can_alert and not p.can_broadcast


def test_posture_market_data_down_blocks_calls_only():
    p = posture(DependencyHealth(market_data=False))
    assert p.can_alert and not p.can_broadcast


def test_posture_budget_exhausted_blocks_calls():
    assert posture(DependencyHealth(budget_ok=False)).can_broadcast is False


# --- recovery freshness ----------------------------------------------------


def test_freshness_gate():
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    assert recovery.is_fresh(now - timedelta(seconds=60), now) is True
    assert recovery.is_fresh(now - timedelta(hours=1), now) is False


# --- admin service ---------------------------------------------------------


async def test_admin_source_lifecycle_forward_only(session_factory):
    account_id = await service.add_source(session_factory, platform_user_id="123", handle="whale", tier="HOT")
    assert account_id > 0
    sources = await service.list_sources(session_factory)
    assert sources[0]["handle"] == "whale" and sources[0]["tier"] == "HOT" and sources[0]["active"] is True

    assert await service.set_tier(session_factory, platform_user_id="123", tier="WARM") is True
    assert await service.remove_source(session_factory, platform_user_id="123") is True
    sources = await service.list_sources(session_factory)
    assert sources[0]["tier"] == "WARM"
    assert sources[0]["active"] is False  # forward-only: stops ingest, keeps history


async def test_admin_kill_engages_killswitch(session_factory):
    await service.kill(session_factory)
    assert await killswitch.is_killed(session_factory) is True
