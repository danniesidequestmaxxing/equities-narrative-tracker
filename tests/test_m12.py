"""M12 hardening: LLM daily budget, watchlist cap, ops report, ledger export."""

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from narrative_tracker import jobs
from narrative_tracker.admin.export import make_export_zip
from narrative_tracker.db import idempotency, repo
from narrative_tracker.extract.stance import FallbackStanceClassifier, LlmStanceClassifier, RuleBasedStanceClassifier
from narrative_tracker.notify.telegram_bot import AlertNotifier
from narrative_tracker.ops.llm_budget import BudgetExhausted, DailyCallBudget, consume_or_raise
from narrative_tracker.schemas.mention import Stance

T0 = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- daily budget

def test_daily_budget_caps_and_reports():
    b = DailyCallBudget(3)
    assert [b.allow() for _ in range(5)] == [True, True, True, False, False]
    assert b.snapshot() == {"used": 3, "limit": 3}
    with pytest.raises(BudgetExhausted):
        consume_or_raise(b)
    consume_or_raise(None)            # no budget configured -> no-op
    assert DailyCallBudget(0).allow() # 0 = uncapped


def test_daily_budget_resets_on_new_day():
    b = DailyCallBudget(1)
    assert b.allow() and not b.allow()
    b._day = (datetime.now(timezone.utc) - timedelta(days=1)).date()  # simulate midnight
    assert b.allow()


async def test_stance_degrades_to_rules_when_budget_exhausted():
    budget = DailyCallBudget(0)
    budget._limit = 1
    budget._used = 1
    budget._day = datetime.now(timezone.utc).date()

    async def infer(text):
        consume_or_raise(budget)
        raise AssertionError("LLM must not be reached over budget")

    clf = FallbackStanceClassifier(LlmStanceClassifier(infer=infer), RuleBasedStanceClassifier())
    out = await clf.classify("long $NVDA ripping")
    assert out.stance is Stance.BULLISH  # rule-based answered, nothing broke


# ------------------------------------------------------------ watchlist cap

async def test_dashboard_add_respects_cap(session_factory, monkeypatch):
    from narrative_tracker.api import dashboard
    fastapi = pytest.importorskip("fastapi")

    monkeypatch.setattr(dashboard, "_sf", session_factory)
    monkeypatch.setattr(dashboard._settings, "max_watchlist", 2)
    await dashboard.api_add_source(dashboard.SourceIn(handle="one"))
    await dashboard.api_add_source(dashboard.SourceIn(handle="two"))
    with pytest.raises(fastapi.HTTPException) as ei:
        await dashboard.api_add_source(dashboard.SourceIn(handle="three"))
    assert ei.value.status_code == 409 and "full" in ei.value.detail
    # re-adding an existing handle is not blocked by the cap
    assert (await dashboard.api_add_source(dashboard.SourceIn(handle="one")))["ok"] is True


# --------------------------------------------------------------- ops report

async def test_ops_report_counts_and_alarm(session_factory, fake_bot):
    notifier = AlertNotifier(bot=fake_bot, session_factory=session_factory, trading_chat_id=7)

    # empty system -> zero-ingest alarm
    out = await jobs.run_ops_report(session_factory, notifier, now=T0)
    assert out["broadcast"] is True and out["alarm"] is True
    assert "ZERO posts" in fake_bot.sent[0]["text"]

    # same UTC date -> deduped
    again = await jobs.run_ops_report(session_factory, notifier, now=T0 + timedelta(hours=3))
    assert again["broadcast"] is False and len(fake_bot.sent) == 1

    # next day with activity -> counts, no alarm
    acct = await repo.get_or_create_account(session_factory, platform_user_id="w", handle="w", tier="HOT")
    pid, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=acct, platform_post_id="1", text="$NVDA", posted_at=T0)
    await repo.add_mentions(session_factory, post_id=pid, mentions=[
        {"symbol": "NVDA", "asset_class": "equity", "stance": "bullish",
         "stance_confidence": 0.9, "mention_confidence": 0.9}])
    budget = DailyCallBudget(2000)
    budget.allow()
    out2 = await jobs.run_ops_report(
        session_factory, notifier, now=T0 + timedelta(days=1), llm_budget=budget)
    assert out2["broadcast"] is True and out2["alarm"] is False and out2["posts"] == 1
    assert "1/2000" in fake_bot.sent[1]["text"]


# ------------------------------------------------------------------- export

async def test_export_zip_contains_ledger(session_factory):
    acct = await repo.get_or_create_account(session_factory, platform_user_id="w", handle="whale", tier="HOT")
    pid, _ = await idempotency.insert_post_if_new(
        session_factory, account_id=acct, platform_post_id="1", text="long $WINR", posted_at=T0)
    from narrative_tracker.db import calls as db_calls
    await db_calls.save_call(
        session_factory, post_id=pid, account_id=acct, symbol="WINR", direction="long",
        entry=10.0, stop=9.0, targets=[12.0], horizon_raw=None, horizon_days=10,
        is_option=False, confidence=0.9, stated_at=T0)

    name, data = await make_export_zip(session_factory)
    assert name.startswith("narrative-tracker-export-") and name.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert set(zf.namelist()) == {"accounts.csv", "mention_outcomes.csv", "explicit_calls.csv"}
        assert "whale" in zf.read("accounts.csv").decode()
        assert "WINR" in zf.read("explicit_calls.csv").decode()
