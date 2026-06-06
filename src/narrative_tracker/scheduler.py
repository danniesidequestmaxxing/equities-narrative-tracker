"""Interval scheduler (M6).

Runs the cadence jobs (digest / recommend / scoring) on intervals. Deliberately
simple: each job has an interval and a last-run stamp; ``tick(now)`` fires the due
ones. A job that raises is logged and skipped (one bad run never wedges the loop).
The jobs themselves enforce pause / kill — the scheduler just decides *when*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class ScheduledJob:
    name: str
    interval_s: float
    run: Callable[[datetime], Awaitable]
    last_run: float = 0.0  # epoch seconds; 0 = never run

    def due(self, now_ts: float) -> bool:
        return (now_ts - self.last_run) >= self.interval_s


class Scheduler:
    def __init__(self, jobs: list[ScheduledJob]) -> None:
        self._jobs = jobs

    async def tick(self, now: datetime) -> list[str]:
        """Run all jobs whose interval has elapsed. Returns the names fired."""
        now_ts = now.timestamp()
        fired: list[str] = []
        for job in self._jobs:
            if not job.due(now_ts):
                continue
            try:
                await job.run(now)
            except Exception:  # noqa: BLE001 - one bad run must not wedge the loop
                log.exception("scheduled job %s failed", job.name)
            job.last_run = now_ts
            fired.append(job.name)
        return fired
