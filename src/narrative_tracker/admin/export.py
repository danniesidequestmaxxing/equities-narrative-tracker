"""On-demand ledger export (M12): /export DMs you a zip of the data that can't
be regenerated — the track record. Belt-and-braces alongside DB backups."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.models import Account, ExplicitCall, MentionOutcome


def _csv(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


async def make_export_zip(sf) -> tuple[str, bytes]:
    """Returns (filename, zip_bytes) with accounts / outcomes / stated calls."""
    async with sf() as session:
        accounts = (await session.scalars(select(Account).order_by(Account.id))).all()
        outcomes = (await session.scalars(select(MentionOutcome).order_by(MentionOutcome.id))).all()
        calls = (await session.scalars(select(ExplicitCall).order_by(ExplicitCall.id))).all()

    files = {
        "accounts.csv": _csv(
            ["handle", "tier", "active", "added_at"],
            [[a.handle, a.tier, a.active, a.added_at] for a in accounts],
        ),
        "mention_outcomes.csv": _csv(
            ["symbol", "stance", "posted_at", "px_post", "fwd_1d", "fwd_3d", "fwd_5d",
             "bench_1d", "bench_3d", "bench_5d", "account_id"],
            [[o.symbol, o.stance, o.posted_at, o.px_post, o.fwd_1d, o.fwd_3d, o.fwd_5d,
              o.bench_1d, o.bench_3d, o.bench_5d, o.account_id] for o in outcomes],
        ),
        "explicit_calls.csv": _csv(
            ["symbol", "direction", "entry", "stop", "targets", "stated_at", "status",
             "close_reason", "realized_r", "realized_pct", "account_id"],
            [[c.symbol, c.direction, c.entry, c.stop, c.targets, c.stated_at, c.status,
              c.close_reason, c.realized_r, c.realized_pct, c.account_id] for c in calls],
        ),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"narrative-tracker-export-{stamp}.zip", buf.getvalue()
