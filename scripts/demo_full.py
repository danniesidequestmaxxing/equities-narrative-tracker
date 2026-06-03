"""End-to-end demo (M0–M5) with fakes only — the whole compounding loop.

    python scripts/demo_full.py

    ingest -> extract -> analyze (sentiment + narratives) -> recommend (gated
    calls) -> broadcast -> score outcome -> recompute account credibility.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

from narrative_tracker.admin import service
from narrative_tracker.analyze.analyzer import Analyzer
from narrative_tracker.analyze.sentiment import credibility_prior
from narrative_tracker.db.base import build_engine, build_sessionmaker, create_all
from narrative_tracker.enrich.market_data import FakeMarketData, MarketSnapshot
from narrative_tracker.extract.pipeline import ExtractionPipeline
from narrative_tracker.notify.telegram_bot import AlertNotifier, build_call
from narrative_tracker.recommend.engine import recommend
from narrative_tracker.recommend.types import RiskConfig
from narrative_tracker.score.credibility import recompute_credibility
from narrative_tracker.scorer import score_call
from narrative_tracker.scorer.types import Bar, Call, Direction


def banner(title: str) -> None:
    print(f"\n{'=' * 64}\n  {title}\n{'=' * 64}")


class PrintBot:
    async def send_message(self, chat_id, text, **kwargs):
        for line in text.splitlines():
            print(f"   │ {line}")
        print("   └" + "─" * 50)

        class _R:
            message_id = 1

        return _R()


# (handle, tier, post_id, text)
WATCHLIST = [("44196397", "whale_trader", "HOT"), ("783214", "macro_mike", "WARM"),
             ("99001", "value_val", "COLD"), ("55012", "chart_chad", "WARM"), ("66020", "pump_pete", "COLD")]
POSTS = [
    ("44196397", "whale_trader", "1", "$NVDA breaking out, loading calls — ai gpu demand insane \U0001f680"),
    ("783214", "macro_mike", "2", "still long $NVDA here, the data center buildout is just starting"),
    ("99001", "value_val", "3", "Nvidia momentum looks strong, accumulating more"),
    ("55012", "chart_chad", "4", "$AVGO ripping too, ai infra trade is on"),
    ("66020", "pump_pete", "5", "$ZZZ to the moon buy now everyone \U0001f680\U0001f680\U0001f680"),
]


async def main() -> None:
    engine = build_engine("sqlite+aiosqlite:///./demo_full.db")
    await create_all(engine)
    sf = build_sessionmaker(engine)
    bot = PrintBot()
    notifier = AlertNotifier(bot=bot, session_factory=sf, trading_chat_id=1)
    pipeline = ExtractionPipeline()
    analyzer = Analyzer()
    tier_by_uid = {uid: tier for uid, _, tier in WATCHLIST}

    banner("1) ADMIN — add watched accounts")
    for uid, handle, tier in WATCHLIST:
        await service.add_source(sf, platform_user_id=uid, handle=handle, tier=tier)
        print(f"   + @{handle} ({tier})")

    banner("2) INGEST + EXTRACT + ANALYZE")
    t = 1_000_000.0
    for uid, handle, _pid, text in POSTS:
        mentions = await pipeline.extract(text=text, source_post_id=_pid)
        cred = credibility_prior(tier_by_uid[uid])
        for m in mentions:
            analyzer.ingest(
                symbol=m.symbol, text=text, stance=m.stance.value,
                stance_confidence=m.stance_confidence, credibility=cred, ts=t,
            )
            print(f"   @{handle}: ${m.symbol} [{m.stance.value} {m.stance_confidence:.2f}] -> {m.asset_class.value}")
        t += 30

    now_ts = t + 60
    banner("3) NARRATIVE DIGEST (derived analysis)")
    mdv2, _ = analyzer.digest(cadence_label="Daily", date_label="03 Jun 2026", now=now_ts, posts_count=len(POSTS), accounts_count=len(WATCHLIST))
    for line in mdv2.splitlines():
        print(f"   │ {line}")

    banner("4) RECOMMEND — build gated calls")
    hot = analyzer.hot_tickers(now_ts, top=5)
    market = FakeMarketData({
        "NVDA": MarketSnapshot("NVDA", "equity", price=150.0, adv_usd=5e8, spread_pct=0.05, market_cap=3.6e12, atr=4.0, as_of=datetime.now(timezone.utc)),
        "AVGO": MarketSnapshot("AVGO", "equity", price=1700.0, adv_usd=2e8, spread_pct=0.1, market_cap=8e11, atr=40.0, as_of=datetime.now(timezone.utc)),
        "ZZZ": MarketSnapshot("ZZZ", "equity", price=0.8, adv_usd=5e4, spread_pct=4.0, market_cap=1e7, atr=0.2, as_of=datetime.now(timezone.utc)),  # illiquid microcap
    })
    inputs = []
    for tkr in hot:
        if tkr["S"] <= 0:
            continue
        inputs.append({
            "symbol": tkr["symbol"], "asset_class": "equity", "stance": "bullish",
            "confidence": 0.9, "stance_confidence": 0.85, "negation_flag": False,
            "extracted_symbols": {tkr["symbol"]}, "net_cred_weighted_stance": tkr["S"],
            "pump_score": 0.9 if tkr["symbol"] == "ZZZ" else 0.1,
            "narrative": "AI infrastructure" if tkr["symbol"] in ("NVDA", "AVGO") else None,
            "source_accounts": ["whale_trader", "macro_mike", "value_val"],
        })
    calls, evals = await recommend(inputs, provider=market, config=RiskConfig(),
                                   now=datetime.now(timezone.utc), date_label="2026-06-03")
    for e in evals:
        verdict = "BROADCAST" if e["passed"] else f"suppressed ({e['suppress_reason']})"
        print(f"   ${e['symbol']}: {verdict}")
    print()
    for call in calls:
        await notifier.broadcast_call(call)

    banner("5) SCORE OUTCOME + RECOMPUTE CREDIBILITY")
    DAY = 86400
    closed: list[dict] = []
    if calls:
        c = calls[0]
        # Simulate the live call hitting its target over daily bars.
        bars = [Bar(n * DAY, D("150"), D("151"), D("149"), D("150")) for n in range(1, 8)]
        bars[5] = Bar(6 * DAY, D("158"), D(str(c.targets.t1 + 1)), D("157"), D(str(c.targets.t1 + 1)))
        scall = Call(c.call_id, c.symbol, Direction.LONG, DAY, D(str(c.entry)), D(str(c.stop)), (D(str(c.targets.t1)),), 10 * DAY)
        out = score_call(scall, bars)
        print(f"   live {c.symbol} call closed: {out.reason.value}  realized R = {out.realized_r:+.2f}\n")
        closed.append({
            "closed_at": 7 * DAY, "open_time": DAY, "R": float(out.realized_r), "bench_R": 0.0, "dir": 1,
            "contribs": [{"account": a, "stance": 1, "conf": 0.9, "mention_time": DAY} for a in c.source_accounts],
        })

    def closed_call(*, accounts, stances, R, closed_at):
        return {
            "closed_at": closed_at, "open_time": closed_at - 2 * DAY, "R": R, "bench_R": 0.0, "dir": 1,
            "contribs": [{"account": a, "stance": s, "conf": 0.9, "mention_time": closed_at - 2 * DAY} for a, s in zip(accounts, stances)],
        }

    # A little prior track record so the loop can differentiate accounts.
    closed += [
        closed_call(accounts=["whale_trader", "macro_mike"], stances=[1, 1], R=2.0, closed_at=2 * DAY),
        closed_call(accounts=["whale_trader"], stances=[1], R=1.5, closed_at=3 * DAY),
        closed_call(accounts=["macro_mike"], stances=[1], R=-1.0, closed_at=4 * DAY),   # macro took a loss
        closed_call(accounts=["value_val"], stances=[1], R=1.0, closed_at=5 * DAY),
    ]
    cred = recompute_credibility(closed, T=8 * DAY)
    print("   realized-outcome credibility (the moat — sharper accounts weigh more in future signals):")
    for acct, score in sorted(cred.items(), key=lambda kv: -kv[1]):
        print(f"     @{acct}: {score:.4f}")

    await engine.dispose()
    banner("DONE — full loop ran: ingest -> extract -> analyze -> gated calls -> score -> credibility")


if __name__ == "__main__":
    asyncio.run(main())
