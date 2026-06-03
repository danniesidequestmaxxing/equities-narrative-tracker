"""Recovery / replay freshness gate (M5).

After an outage, a backlog of posts may flood in. The freshness gate keeps replay
from firing stale real-time alerts/calls — only posts within ``max_age_s`` are
eligible (older posts still count toward narrative tallies, just not alerts).
"""

from __future__ import annotations

from datetime import datetime


def is_fresh(posted_at: datetime, now: datetime, *, max_age_s: int = 300) -> bool:
    age = (now - posted_at).total_seconds()
    return 0 <= age <= max_age_s
