"""M4: credibility recomputation, closure-time invariant, attribution."""

from datetime import datetime, timezone

from narrative_tracker.db import repo
from narrative_tracker.score.credibility import attribute_call, recompute_credibility

DAY = 86400


def _call(*, account, win, closed_at, open_time=0.0, direction=1, conf=0.9):
    return {
        "closed_at": closed_at,
        "open_time": open_time,
        "R": 2.0 if win else -1.0,
        "bench_R": 0.0,
        "dir": direction,
        "contribs": [{"account": account, "stance": direction, "conf": conf, "mention_time": open_time}],
    }


def test_profitable_account_outranks_loser():
    calls = [_call(account="sharp", win=True, closed_at=i * DAY) for i in range(1, 6)]
    calls += [_call(account="bad", win=False, closed_at=i * DAY) for i in range(1, 6)]
    cred = recompute_credibility(calls, T=10 * DAY)
    assert cred["sharp"] > cred["bad"]


def test_closure_time_invariant():
    """An account's credibility at issuance must not incorporate outcomes that
    close *after* the issuance time (the moat's correctness condition)."""
    t0 = 100 * DAY
    base = [
        _call(account="A", win=True, closed_at=t0 - 2 * DAY, open_time=t0 - 3 * DAY),
        _call(account="A", win=True, closed_at=t0 - 1 * DAY, open_time=t0 - 2 * DAY),
    ]
    # A second call A drove, which only CLOSES (a win) 14 days after issuance.
    future_win = _call(account="A", win=True, closed_at=t0 + 14 * DAY, open_time=t0)
    open2 = t0 + 3 * DAY

    cred_with_future_in_list = recompute_credibility(base + [future_win], T=open2)
    cred_without = recompute_credibility(base, T=open2)
    # Identical: the not-yet-closed win is filtered out by closure-time.
    assert cred_with_future_in_list == cred_without
    # And once it HAS closed, it does count (sanity).
    assert recompute_credibility(base + [future_win], T=t0 + 20 * DAY)["A"] >= cred_without["A"]


def test_attribution_rewards_correct_contrarian():
    # A LONG call that LOST. The account that was SHORT correctly faded it.
    call = {
        "closed_at": 1, "open_time": 0, "R": -1.0, "bench_R": 0.0, "dir": 1,
        "contribs": [
            {"account": "bull", "stance": 1, "conf": 1.0, "mention_time": 0},
            {"account": "bear", "stance": -1, "conf": 1.0, "mention_time": 0},
        ],
    }
    attr = attribute_call(call)
    assert attr["bull"] < 0  # was long, call lost -> penalized
    assert attr["bear"] > 0  # was short, call lost -> rewarded


async def test_account_score_persist_and_read(session_factory):
    account_id = await repo.get_or_create_account(
        session_factory, platform_user_id="u1", handle="sharp", tier="HOT"
    )
    # No score yet -> tier prior.
    prior = await repo.get_credibility(session_factory, account_id=account_id, as_of=datetime.now(timezone.utc))
    assert prior == 0.6  # HOT prior

    await repo.insert_account_score(
        session_factory,
        account_id=account_id,
        as_of=datetime(2026, 6, 3, tzinfo=timezone.utc),
        decayed_score=0.42,
        sample_size=12,
    )
    got = await repo.get_credibility(
        session_factory, account_id=account_id, as_of=datetime(2026, 6, 4, tzinfo=timezone.utc)
    )
    assert got == 0.42
