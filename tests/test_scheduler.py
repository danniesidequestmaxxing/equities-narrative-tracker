"""M6: interval scheduler."""

from datetime import datetime, timedelta, timezone

from narrative_tracker.scheduler import ScheduledJob, Scheduler

T0 = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)


async def test_scheduler_fires_due_jobs_on_interval():
    fired = []

    async def job(now):
        fired.append(now)

    s = Scheduler([ScheduledJob("j", interval_s=3600, run=job)])
    assert await s.tick(T0) == ["j"]                                  # never run -> due
    assert await s.tick(T0 + timedelta(seconds=60)) == []             # not due yet
    assert await s.tick(T0 + timedelta(seconds=3700)) == ["j"]        # interval elapsed
    assert len(fired) == 2


async def test_scheduler_isolates_job_failures():
    ran = []

    async def boom(now):
        raise RuntimeError("kaboom")

    async def good(now):
        ran.append(1)

    s = Scheduler([ScheduledJob("boom", 1, boom), ScheduledJob("good", 1, good)])
    fired = await s.tick(T0)
    assert "good" in fired and ran == [1]  # a failing job never blocks the others
