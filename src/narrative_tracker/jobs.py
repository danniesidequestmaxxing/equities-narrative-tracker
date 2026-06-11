"""Cadence jobs (M6): digest, recommend, scoring.

These run on a schedule (see scheduler.py) and turn the standing Analyzer state +
market data into broadcasts and graded outcomes — the part of the product beyond
real-time alerts. All deps are injected so the jobs are testable with fakes, and
all respect the kill switch / pause state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .analyze import outcomes as outcomes_math
from .analyze import pulse as pulse_mod
from .analyze.analyzer import Analyzer
from .analyze.narratives import assign_narratives
from .analyze.technicals import snapshot_from_bars
from .db import analytics, recs, repo
from .db import calls as db_calls
from .db import outcomes as db_outcomes
from .notify.escaping import md, md_code
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

    div = await analytics.divergence(sf, since=since, limit=3)

    bucket = f"{now:%Y-%m-%d}:{int(now.hour // max(1, int(hours)))}"
    mdv2, plain = pulse_mod.build_pulse(
        window_label=f"{hours:g}h",
        date_label=f"{now:%Y-%m-%d %H:%M} UTC",
        posts_count=len({r["platform_post_id"] for r in window}),
        accounts_count=len(posted_handles),
        tickers_count=len({r["symbol"] for r in window}),
        hot=hot, brief=brief, narratives=pulse_mod.fallback_narratives(window),
        early=early, deep_dives=deep_dives, recap=recap, quiet=quiet,
        market_hint=market is None, llm_hint=writer is None, divergence=div,
    )
    sent = False
    if broadcast:
        sent = await notifier.broadcast_text(idempotency_key=f"PULSE:{bucket}", mdv2=mdv2, plain=plain)
    return {
        "broadcast": sent, "posts": len({r["platform_post_id"] for r in window}),
        "tickers": len({r["symbol"] for r in window}), "deep_dives": len(deep_dives),
        "llm": brief is not None, "early": [e["symbol"] for e in early],
    }


async def run_outcomes(
    sf, market, *, now: datetime, benchmark: str = "SPY", lookback_days: int = 45,
    bars_days: int = 150, throttle_s: float = 0.0, source: str = "massive-adj",
) -> dict:
    """M9 event-study: refresh split-adjusted bars for every mentioned symbol
    (+ the benchmark) and (re)compute forward returns for each mention until
    complete. Backfills the whole stored history the first time it runs.

    One bars fetch per symbol per run; ``throttle_s`` keeps the free data tier
    happy. Symbols whose bars can't be fetched are skipped and retried next run.
    """
    from .db import bars as db_bars

    pend = await db_outcomes.mentions_needing_outcomes(sf, since=now - timedelta(days=lookback_days))
    if not pend:
        return {"computed": 0, "pending": 0, "symbols": 0}

    symbols = sorted({m["symbol"] for m in pend})
    bars_by: dict[str, list[dict]] = {}
    for sym in [benchmark, *symbols]:
        try:
            bars = await market.fetch_bars(sym, days=bars_days, adjusted=True)
            if bars:
                bars_by[sym] = bars
                await db_bars.save_bars(sf, symbol=sym, interval="1d", source=source, bars=bars)
        except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
            log.warning("outcomes: bars fetch failed for %s: %s", sym, exc)
        if throttle_s:
            await asyncio.sleep(throttle_s)

    # The benchmark is central to the edge metric — without it, rows would be
    # marked complete benchless forever. Defer mention outcomes to the next
    # cycle (e.g. boot-time 429 bursts on the free tier) rather than degrade.
    bench_bars = bars_by.get(benchmark)
    computed = 0
    if bench_bars is None:
        log.warning("outcomes: benchmark %s bars unavailable; deferring mention outcomes", benchmark)
    else:
        for m in pend:
            bars = bars_by.get(m["symbol"])
            if not bars:
                continue
            out = outcomes_math.forward_returns(bars, m["posted_at"])
            if out is None:
                continue  # post newer than the latest close; try again next run
            bench = outcomes_math.forward_returns(bench_bars, m["posted_at"])
            await db_outcomes.upsert_outcome(
                sf, mention_id=m["mention_id"], account_id=m["account_id"], symbol=m["symbol"],
                stance=m["stance"], posted_at=m["posted_at"], px_post=out["px_post"],
                fwd=out["fwd"], bench=(bench or {}).get("fwd"),
            )
            computed += 1

    # M9-C: grade open stated calls against the same bars — which came first,
    # their stop, their target, or the timeout?
    stated_closed = 0
    for c in await db_calls.open_calls(sf):
        bars = bars_by.get(c["symbol"]) or await _adj_bars_from_db(sf, c["symbol"], source)
        if not bars:
            continue
        res = outcomes_math.stated_call_outcome(
            bars, stated_at=c["stated_at"], direction=c["direction"], entry=c["entry"],
            stop=c["stop"], targets=c["targets"], horizon_days=c["horizon_days"],
        )
        if res is None:
            continue  # still open
        await db_calls.close_call(
            sf, call_id=c["id"], reason=res["reason"], realized_r=res["realized_r"],
            realized_pct=res["realized_pct"],
            closed_at=datetime.fromtimestamp(res["closed_ts"], tz=timezone.utc),
        )
        stated_closed += 1
    # M10: fold fresh evidence into live credibility — accounts that keep being
    # right get louder everywhere (sentiment, pulse, recommendations).
    cred_updated = await refresh_account_credibility(sf, now=now)
    return {
        "computed": computed, "pending": len(pend) - computed,
        "symbols": len(symbols), "stated_closed": stated_closed,
        "credibility_updated": cred_updated,
    }


async def refresh_account_credibility(sf, *, now: datetime, window_days: int = 90) -> int:
    """Recompute evidence-weighted credibility from the trailing M9 ledger and
    append point-in-time account_scores rows (read by sentiment + analytics)."""
    from .db.scoreboard import _aggregate
    from .score.credibility import evidence_credibility

    since = now - timedelta(days=window_days)
    rows = await db_outcomes.outcomes_for_accounts(sf, since=since)
    board = _aggregate(rows, min_n=1)
    by_handle = {a["handle"]: a for a in board["ranked"] + board["thin"]}
    stated = await db_calls.stated_stats(sf, since=since)

    updated = 0
    for acct in await repo.all_accounts(sf):
        ev = by_handle.get(acct["handle"])
        st = stated.get(acct["handle"])
        if ev is None and st is None:
            continue  # no evidence -> the tier prior stands, write nothing
        score = evidence_credibility(
            acct["tier"],
            event_n=ev["n"] if ev else 0,
            event_edge=ev["avg_3d"] if ev else None,
            stated_n=st["closed"] if st else 0,
            stated_avg_r=st["avg_r"] if st else None,
            stated_hit=st["hit"] if st else None,
        )
        await repo.insert_account_score(
            sf, account_id=acct["id"], as_of=now, decayed_score=score,
            sample_size=(ev["n"] if ev else 0) + (st["closed"] if st else 0),
        )
        updated += 1
    return updated


async def _adj_bars_from_db(sf, symbol: str, source: str) -> list[dict]:
    """Cached adjusted bars (saved by run_outcomes) as plain dicts."""
    from .db import bars as db_bars

    rows = await db_bars.load_bars(sf, symbol=symbol, interval="1d", source=source)
    return [{"ts": b.ts, "o": float(b.o), "h": float(b.h), "l": float(b.l), "c": float(b.c)} for b in rows]


def _fmt_level(v: float | None) -> str:
    return "market" if v is None else f"{v:,.2f}"


async def run_call_scan(
    sf, extractor, notifier: AlertNotifier | None = None,
    *, batch: int = 40, min_confidence: float = 0.6,
) -> dict:
    """M9-C rolling scan: extract explicit calls from posts not yet scanned —
    backfills the whole history in batches, then keeps up with new posts.
    A post that errors is left unscanned and retried next cycle."""
    posts = await db_calls.unscanned_posts(sf, limit=batch)
    if not posts:
        return {"scanned": 0, "calls": 0}

    scanned_ids: list[int] = []
    saved = 0
    for post in posts:
        try:
            extraction = await extractor(post["text"])
        except Exception as exc:  # noqa: BLE001 - retry this post next cycle
            log.warning("call scan failed for post %s: %s", post["id"], exc)
            continue
        scanned_ids.append(post["id"])
        if not extraction.has_call:
            continue
        for call in extraction.calls:
            sym = call.symbol.strip().lstrip("$").upper()
            if not sym or call.confidence < min_confidence:
                continue
            created = await db_calls.save_call(
                sf, post_id=post["id"], account_id=post["account_id"], symbol=sym,
                direction=call.direction, entry=call.entry, stop=call.stop,
                targets=list(call.targets or []), horizon_raw=call.horizon,
                horizon_days=_horizon_days(call.horizon), is_option=call.is_option,
                confidence=call.confidence, stated_at=post["posted_at"],
            )
            if not created:
                continue
            saved += 1
            if notifier is not None:
                t1 = call.targets[0] if call.targets else None
                handle = post["handle"] or "source"
                url = f"https://x.com/{handle}/status/{post['platform_post_id']}"
                snippet = " ".join((post["text"] or "").split())[:160]
                mdv2 = (
                    f"\U0001F3AF *Stated call* · `{md_code('$' + sym)}` · "
                    f"*{md(call.direction.upper())}* by @{md(handle)}\n"
                    f"entry `{md_code(_fmt_level(call.entry))}` · stop `{md_code(_fmt_level(call.stop) if call.stop else '—')}` · "
                    f"target `{md_code(_fmt_level(t1) if t1 else '—')}`\n"
                    f"“_{md(snippet)}_”\n"
                    f"[\U0001F517 Post]({url})\n\n_Tracked for the scoreboard · not financial advice_"
                )
                plain = (
                    f"[STATED CALL] ${sym} {call.direction.upper()} by @{handle}\n"
                    f"entry {_fmt_level(call.entry)} | stop {_fmt_level(call.stop) if call.stop else '-'} | "
                    f"target {_fmt_level(t1) if t1 else '-'}\n\"{snippet}\"\n{url}"
                )
                await notifier.broadcast_text(
                    idempotency_key=f"XCALL:{post['id']}:{sym}", mdv2=mdv2, plain=plain
                )
    await db_calls.mark_scanned(sf, scanned_ids)
    return {"scanned": len(scanned_ids), "calls": saved, "remaining_batch": len(posts) - len(scanned_ids)}


def _horizon_days(raw: str | None) -> int:
    from .extract.calls_llm import horizon_days

    return horizon_days(raw)


async def run_weekly_report(sf, notifier: AlertNotifier, *, now: datetime) -> dict:
    """M11: the Sunday ritual — who was right this week, which stated calls
    graded, what ran hottest. One idempotent broadcast per ISO week; silent
    when there is nothing to report yet."""
    from .db import scoreboard as db_scoreboard

    if await killswitch.is_killed(sf) or await killswitch.get_pause(sf) != killswitch.PAUSE_NONE:
        return {"skipped": "killed_or_paused"}

    since = now - timedelta(days=7)
    board = await db_scoreboard.account_scoreboard(sf, since=since)
    graded = await db_calls.closed_calls(sf, since=since)
    hot = await analytics.hot_tickers(sf, since=since, limit=5)
    if not board["ranked"] and not graded and not hot:
        return {"skipped": "no_data"}

    iso = now.isocalendar()
    L = [f"\U0001f4c5 *Weekly alpha report* — week {iso.week}, {md(f'{now:%Y-%m-%d}')}"]

    L += ["", "*\U0001f3c6 Who was right \\(7d edge vs SPY, 3\\-day\\)*"]
    if board["ranked"]:
        for i, a in enumerate(board["ranked"][:5], 1):
            hit = f"{a['hit_3d'] * 100:.0f}%" if a["hit_3d"] is not None else "—"
            avg = md(f"{(a['avg_3d'] or 0) * 100:+.1f}%")
            L.append(f"{i}\\. @{md(a['handle'])} · n={a['n']} · hit {md(hit)} · avg {avg}")
    else:
        L.append("_not enough graded mentions this week_")

    L += ["", "*\U0001f3af Stated calls graded this week*"]
    if graded:
        for c in graded[:6]:
            res = f"{c['realized_r']:+.1f}R" if c["realized_r"] is not None else (
                f"{(c['realized_pct'] or 0) * 100:+.1f}%")
            emoji = "✅" if (c["realized_pct"] or 0) > 0 else "❌"
            L.append(f"{emoji} `{md_code('$' + c['symbol'])}` {md(c['direction'])} by @{md(c['handle'])} "
                     f"→ {md(res)} \\({md(c['reason'] or '')}\\)")
    else:
        L.append("_none closed this week_")

    L += ["", "*\U0001f525 Hottest names*"]
    if hot:
        L.append(md(" · ".join(f"${t['symbol']} ({t['mentions']}m)" for t in hot)))
    else:
        L.append("_quiet week_")

    L += ["", "_Analysis only · derived metrics · not financial advice_"]
    mdv2 = "\n".join(L)
    plain = (
        f"[WEEKLY] week {iso.week} {now:%Y-%m-%d}\n"
        + "Top: " + ", ".join(f"@{a['handle']} {(a['avg_3d'] or 0) * 100:+.1f}%" for a in board["ranked"][:5])
        + "\nGraded: " + (", ".join(
            f"${c['symbol']} {(c['realized_pct'] or 0) * 100:+.1f}%" for c in graded[:6]) or "none")
        + "\nHot: " + ", ".join(f"${t['symbol']}" for t in hot)
    )
    sent = await notifier.broadcast_text(
        idempotency_key=f"WEEKLY:{iso.year}-W{iso.week}", mdv2=mdv2, plain=plain
    )
    return {"broadcast": sent, "ranked": len(board["ranked"]), "graded": len(graded)}


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
