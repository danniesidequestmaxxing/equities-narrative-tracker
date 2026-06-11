"""Cadence jobs (M6): digest, recommend, scoring.

These run on a schedule (see scheduler.py) and turn the standing Analyzer state +
market data into broadcasts and graded outcomes — the part of the product beyond
real-time alerts. All deps are injected so the jobs are testable with fakes, and
all respect the kill switch / pause state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .analyze import pulse as pulse_mod
from .analyze.analyzer import Analyzer
from .analyze.narratives import assign_narratives
from .analyze.technicals import snapshot_from_bars
from .db import analytics, recs, repo
from .enrich.market_data import MarketDataProvider
from .extract.symbology import classify_symbol
from .notify.telegram_bot import AlertNotifier
from .ops import killswitch
from .recommend.engine import recommend
from .recommend.types import RiskConfig
from .score.credibility import recompute_credibility
from .scorer import score_call
from .scorer.types import Call as ScoreCall
from .scorer.types import Direction as ScoreDir

log = logging.getLogger(__name__)


async def run_digest(
    sf, analyzer: Analyzer, notifier: AlertNotifier,
    *, cadence_label: str, date_label: str, now_ts: float, broadcast: bool = True,
) -> dict:
    mdv2, plain = analyzer.digest(cadence_label=cadence_label, date_label=date_label, now=now_ts)
    sent = False
    if broadcast and not await killswitch.is_killed(sf) and await killswitch.get_pause(sf) == killswitch.PAUSE_NONE:
        sent = await notifier.broadcast_text(
            idempotency_key=f"DIGEST:{date_label}:{cadence_label}", mdv2=mdv2, plain=plain
        )
    return {"broadcast": sent}


async def run_pulse(
    sf, notifier: AlertNotifier,
    *, now: datetime, hours: float = 8.0, market=None, writer: pulse_mod.PulseWriter | None = None,
    watchlist_provider=None, broadcast: bool = True, deep_dive_n: int = 3,
) -> dict:
    """The 8-hour investor briefing: account recap, hot tickers vs the prior
    window, early radar, TA + fundamentals deep-dive, narrative brief.

    Built from durable DB state (not the in-memory Analyzer), so it's complete
    even right after a restart. One broadcast per window (idempotency key is the
    window bucket), and a window that produced no posts sends nothing.
    """
    if await killswitch.is_killed(sf) or await killswitch.get_pause(sf) != killswitch.PAUSE_NONE:
        return {"skipped": "killed_or_paused"}

    since = now - timedelta(hours=hours)
    recent = await analytics._mention_rows(sf, since=now - timedelta(hours=2 * hours))
    window = [r for r in recent if pulse_mod._utc(r["posted_at"]) >= since]
    if not window:
        return {"skipped": "no_posts"}
    prev_counts: dict[str, int] = {}
    for r in recent:
        if pulse_mod._utc(r["posted_at"]) < since:
            prev_counts[r["symbol"]] = prev_counts.get(r["symbol"], 0) + 1

    week = await analytics._mention_rows(sf, since=now - timedelta(days=7))
    prior_week = [r for r in week if pulse_mod._utc(r["posted_at"]) < since]

    hot = await analytics.hot_tickers(sf, since=since, limit=8)
    for t in hot:
        prev = prev_counts.get(t["symbol"], 0)
        t["delta"] = "new" if prev == 0 else "up" if t["mentions"] > prev else "down" if t["mentions"] < prev else "flat"

    early = pulse_mod.early_radar(window, prior_week, window_hours=hours)
    recap = pulse_mod.account_recap(window)

    # TA + fundamentals: user-watched tickers first (always included), then the
    # top chartable (equity) names from the window.
    watched = (await repo.watched_tickers(sf))[:5]
    deep_dives: list[dict] = []
    if market is not None:
        candidates: list[tuple[str, bool]] = [(s, True) for s in watched]
        for t in hot:
            if t["asset_class"] == "equity" and t["symbol"] not in watched:
                candidates.append((t["symbol"], False))
        cap = max(deep_dive_n, len(watched))
        for sym, is_watched in candidates:
            if len(deep_dives) >= cap:
                break
            try:
                ta = snapshot_from_bars(await market.fetch_bars(sym, days=400, adjusted=True))
                if ta is None:
                    continue
                overview = await market.fetch_overview(sym)
                deep_dives.append({"symbol": sym, "ta": ta, "watched": is_watched, **overview})
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the pulse
                log.warning("pulse deep-dive failed for %s: %s", sym, exc)

    brief = None
    if writer is not None:
        try:
            brief = await writer(pulse_mod.build_writer_context(window, hot, early, hours=hours))
        except Exception as exc:  # noqa: BLE001 - degrade to the seed-theme fallback
            log.warning("pulse LLM writer failed (%s); using fallback narratives", exc)

    active = list(await watchlist_provider()) if watchlist_provider else []
    posted_handles = {r["handle"] for r in window}
    quiet = sorted(h for h in active if h not in posted_handles)

    bucket = f"{now:%Y-%m-%d}:{int(now.hour // max(1, int(hours)))}"
    mdv2, plain = pulse_mod.build_pulse(
        window_label=f"{hours:g}h",
        date_label=f"{now:%Y-%m-%d %H:%M} UTC",
        posts_count=len({r["platform_post_id"] for r in window}),
        accounts_count=len(posted_handles),
        tickers_count=len({r["symbol"] for r in window}),
        hot=hot, brief=brief, narratives=pulse_mod.fallback_narratives(window),
        early=early, deep_dives=deep_dives, recap=recap, quiet=quiet,
        market_hint=market is None, llm_hint=writer is None,
    )
    sent = False
    if broadcast:
        sent = await notifier.broadcast_text(idempotency_key=f"PULSE:{bucket}", mdv2=mdv2, plain=plain)
    return {
        "broadcast": sent, "posts": len({r["platform_post_id"] for r in window}),
        "tickers": len({r["symbol"] for r in window}), "deep_dives": len(deep_dives),
        "llm": brief is not None, "early": [e["symbol"] for e in early],
    }


def _recommend_inputs(analyzer: Analyzer, now_ts: float, config: RiskConfig) -> list[dict]:
    inputs = []
    for t in analyzer.hot_tickers(now_ts, top=10):
        s, sym = t["S"], t["symbol"]
        if abs(s) < config.conflict_eps:
            continue
        contribs = [
            {"account": c["account"], "stance": c["stance"], "conf": c["conf"], "mention_time": c["ts"]}
            for c in analyzer.contributors_for(sym)
            if c["account"]
        ]
        narrs = assign_narratives(sym)
        inputs.append({
            "symbol": sym,
            "asset_class": analyzer.asset_class.get(sym) or classify_symbol(sym, ""),
            "stance": "bullish" if s > 0 else "bearish",
            "confidence": 0.95,
            "stance_confidence": round(min(0.99, 0.55 + abs(s)), 2),
            "negation_flag": False,
            "extracted_symbols": set(analyzer.sentiment.symbols()),
            "net_cred_weighted_stance": s,
            "pump_score": 0.1,
            "narrative": narrs[0] if narrs else None,
            "source_accounts": list({c["account"] for c in contribs}),
            "_contribs": contribs,
        })
    return inputs


async def run_recommend(
    sf, analyzer: Analyzer, market_provider: MarketDataProvider, notifier: AlertNotifier,
    config: RiskConfig, *, now: datetime, date_label: str, broadcast: bool = True,
    paper: bool = False, max_calls: int = 3, horizon: str = "swing · 1-3w",
) -> dict:
    if await killswitch.is_killed(sf):
        return {"skipped": "killed"}
    inputs = _recommend_inputs(analyzer, now.timestamp(), config)
    contribs_by = {i["symbol"]: i["_contribs"] for i in inputs}
    live = await recs.live_symbols(sf)
    calls, evals = await recommend(
        inputs, provider=market_provider, config=config, now=now, date_label=date_label,
        live_call_symbols=live, max_calls=max_calls, horizon=horizon,
    )
    can_broadcast = broadcast and await killswitch.get_pause(sf) == killswitch.PAUSE_NONE
    calls_by = {c.symbol: c for c in calls}
    sent = paper_calls = suppressed = 0
    for ev in evals:
        call = calls_by.get(ev["symbol"]) if ev["passed"] else None
        if call is not None:
            await recs.save_recommendation(
                sf, call=call, credibility_at_issuance=0.0,
                sources=contribs_by.get(call.symbol, []), gates=ev["gates"], issued_at=now,
            )
            if paper:
                # Track + score it (builds a real record) but DON'T broadcast.
                await recs.mark_live(sf, call_id=call.call_id)
                paper_calls += 1
            elif can_broadcast and await notifier.broadcast_call(call):
                await recs.mark_live(sf, call_id=call.call_id)
                sent += 1
        else:
            suppressed += 1
            await recs.save_suppressed(sf, symbol=ev["symbol"], gates=ev["gates"])
    return {"calls": len(calls), "broadcast": sent, "paper": paper_calls, "suppressed": suppressed}


async def run_refresh_bars(sf, provider, symbols, *, source: str = "polygon", days: int = 400) -> dict:
    """Ingest unadjusted bars + corporate actions for the given symbols."""
    from .db import bars as db_bars

    refreshed = 0
    for sym in symbols:
        await db_bars.save_bars(sf, symbol=sym, interval="1d", source=source, bars=await provider.fetch_bars(sym, days=days))
        await db_bars.save_adjustments(sf, symbol=sym, source=source, adjustments=await provider.fetch_adjustments(sym))
        refreshed += 1
    return {"refreshed": refreshed}


async def run_scoring(
    sf, bars_provider, *, now: datetime, max_age_s: int, bench_provider=None, ledger_provider=None,
) -> dict:
    due = await recs.due_for_scoring(sf, now=now, max_age_s=max_age_s)
    closed = 0
    for rec in due:
        bars = await bars_provider(rec.symbol)
        if not bars:
            continue
        ledger = await ledger_provider(rec.symbol) if ledger_provider else ()
        # SQLite drops tz; treat stored issued_at as UTC for a consistent epoch.
        issued = rec.issued_at if rec.issued_at.tzinfo else rec.issued_at.replace(tzinfo=timezone.utc)
        scall = ScoreCall(
            rec.call_id, rec.symbol, ScoreDir(rec.direction), int(issued.timestamp()),
            Decimal(str(rec.entry)), Decimal(str(rec.stop)),
            (Decimal(str(rec.targets.get("t1"))),), max_age_s,
        )
        bench = await bench_provider(rec.symbol) if bench_provider else ()
        out = score_call(scall, bars, bench, ledger=ledger)
        if out.status != "scored":
            continue
        await recs.close_recommendation(
            sf, rec_id=rec.id, close_reason=out.reason.value, realized_r=float(out.realized_r),
            mfe_r=float(out.mfe_r), mae_r=float(out.mae_r),
            benchmark_r=(float(out.bench_r) if out.bench_r is not None else None), closed_at=now,
        )
        closed += 1

    credibility_updated = 0
    if closed:
        all_closed = await recs.closed_calls_for_credibility(sf)
        cred = recompute_credibility(all_closed, T=now.timestamp())
        for platform_user_id, score in cred.items():
            account_id = await repo.get_account_id(sf, platform_user_id=platform_user_id)
            if account_id is not None:
                await repo.insert_account_score(
                    sf, account_id=account_id, as_of=now, decayed_score=score, sample_size=1
                )
                credibility_updated += 1
    return {"closed": closed, "credibility_updated": credibility_updated}
