# Brainstorm: Equities Narrative Tracker

- **Date:** 2026-06-03
- **Status:** Brainstorm complete → ready for `/ce:plan`
- **Author:** tzyhwan + Claude (compound-engineering)

---

## What We're Building

A system that turns a curated set of **X/Twitter accounts** into **auditable, explicit trade calls** delivered through **Telegram**, and that **grades its own calls over time** so it gets sharper the longer it runs.

Four pillars (from the original spec):

1. **Real-time signal capture** — watch curated X accounts, detect when a post is *about a ticker* (equity, option, or crypto), and push a notification to a Telegram channel within <60s.
2. **Narrative aggregation** — on daily / 3-day / weekly cadences, summarize which tickers each account posted and produce an in-depth read on the **hottest narratives** people are actually trading.
3. **Deep-dive + recommendation** — for the top 5–10 tickers, run technical + fundamental analysis (with a TradingView deep-link per ticker) and issue **explicit buy/sell calls** on the best 3.
4. **Informed-operator loop** — let the user add/filter sources live, analyze social sentiment, and surface the best **momentum + asymmetric** trades for the week.

**The differentiator (and the part most builds skip):** a closed **feedback loop** that logs every call with entry/stop/target, scores realized outcomes, and **weights each X account by its historical accuracy** — so signal quality compounds instead of staying flat.

---

## Why This Approach

**Modular monolith + background worker, Python-centric.** At "me + small group" scale, one deployable service with clean internal stage boundaries (ingest → extract → enrich → analyze → recommend → notify → score) is the fastest path that still splits into services later. A long-running worker (not serverless cron) is required because **<60s latency beats the 1-minute floor** of cron schedulers. Python wins because the whole value sits in TA (`pandas-ta`), backtesting/scoring (`vectorbt`), data wrangling, and LLM analysis — all Python-first. A thin TypeScript dashboard can come later if we want a web UI.

---

## Resolved Decisions

| Area | Decision |
|---|---|
| Audience | Me + small group (shared Telegram channel) |
| X data access | Third-party API (e.g. twitterapi.io / apify) behind a pluggable provider interface + fallback |
| Recommendation posture | **Explicit buy/sell calls** (entry / stop / target) |
| Budget | $300+/mo — premium data + heavy LLM on the table |
| Asset universe | **Everything** — US equities + options + crypto |
| Alert latency | **Near real-time (<60s)** → aggressive polling, heavy dedupe |
| Feedback loop | **Yes, from v1** — outcome scoring + account credibility weighting |
| Chart output | **TradingView deep-link** per ticker (no rendered images) |
| Architecture | **Modular monolith + worker** |
| Stack | Python core (FastAPI admin/health + asyncio worker); Postgres + Redis; thin TS dashboard later |
| Asset rollout | **All three at once** |
| Broadcast gate | **Auto-broadcast** (no human approval) — *with mandatory safety rails, see below* |
| Go-live | **Live from day one** (no shadow/paper period) |
| Seed accounts | User provides the watchlist |

---

## Architecture Sketch (stages)

```
            ┌─────────── Background Worker (always-on) ───────────┐
X accounts → │ 1. INGEST (poll ~15–30s, dedupe, spam/RT filter)    │
            │ 2. EXTRACT (cashtag regex + NER + LLM + vision/OCR) │ → Postgres (system of record)
            │ 3. ENRICH (market data: px/vol/options IV/crypto)   │ ← Redis (dedupe, rate-limit, budget)
            │ 4. ANALYZE (narrative clustering, sentiment, TA/FA) │
            │ 5. RECOMMEND (rank → 3 calls + entry/stop/target)   │
            │ 6. NOTIFY (Telegram: alerts + cadence digests)      │
            │ 7. SCORE (outcomes → account credibility weights)   │
            └─────────────────────────────────────────────────────┘
   FastAPI: /health, /admin (add/remove/filter sources), kill-switch, audit log
```

**Core data model (system of record):** `accounts` (+ credibility score), `posts`, `ticker_mentions`, `narratives`, `market_snapshots`, `recommendations` (entry/stop/target/confidence), `outcomes` (realized P&L vs plan), `audit_log`.

---

## Phasing / Milestones

Even with "all at once," build in dependency order:

- **M0 — Pipeline spine:** ingest → extract (cashtags first) → Postgres → basic Telegram alert. Prove <60s end-to-end on equities.
- **M1 — Full extraction:** NER + LLM disambiguation + **vision/OCR for chart-image posts** (huge fraction of trader signal is images, not text). Add options + crypto symbol resolution.
- **M2 — Narrative + sentiment layer:** embedding-based clustering of tickers into themes; narrative-momentum (rising/peaking/fading); contrarian sentiment-extreme detection.
- **M3 — Deep-dive + calls:** TA/FA enrichment, ranking, explicit 3-call output with TradingView links + safety rails.
- **M4 — Feedback loop:** outcome scoring, account credibility weighting, narrative-accuracy dashboard.

---

## What You're Missing — Areas to Add

The single most valuable output of this brainstorm. Grouped by theme.

### Signal quality
- **Multimodal extraction is mandatory, not optional.** A large share of trader posts are *chart screenshots* with little/no text. Without vision/OCR you silently miss most of the signal. (Promoted to M1.)
- **Ticker disambiguation is hard.** `$AAPL` is easy; "the GLP-1 names," "Zuck's company," or "$X" (ticker vs. the word "times") need NER + LLM + context. Apple-fruit vs Apple-Inc. Budget for a disambiguation pass and a confidence score per mention.
- **Coordinated-pump detection.** The same low-credibility tickers appearing across many accounts simultaneously is usually a *pump*, not alpha. Detect and flag/suppress.
- **Sentiment ≠ alpha.** Universal euphoria on a name is often the top. Track sentiment *extremes* as a contrarian input, not just "more mentions = more bullish."
- **Narrative layer is the real product.** Clustering tickers into themes (AI, GLP-1, uranium, quantum, stablecoins…) and tracking theme momentum is more valuable than per-ticker counts. Lightly specified in the original — make it first-class.

### Risk & tradeability
- **Risk management is absent from the spec.** Explicit calls need position sizing, stop placement, max risk per trade, and **portfolio-level correlation** (don't recommend 3 names that are the same narrative = one concentrated bet).
- **Options need IV awareness.** Don't recommend buying premium when IV is crushed-high into earnings. Pull IV rank/percentile.
- **Tradeability/liquidity filters.** Microcaps and obscure tokens can be untradeable or trivially manipulated. Filter by volume / float / spread so calls are actually executable.
- **Catalyst calendar.** Earnings, FDA/PDUFA, FOMC, token unlocks, OPEX — a momentum call *into* a print is a different risk. Pull an events calendar and annotate calls.

### Trust & compliance (elevated because the group acts on calls)
- **Mandatory safety rails** (given auto-broadcast + live day-one): "not financial advice" disclaimer on every call; **immutable audit log**; **circuit-breakers** that suppress a call on low confidence or coordinated-pump detection; one-tap **kill switch**.
- **Data redistribution rights.** Market-data vendor terms may forbid reposting their data to a Telegram group. Confirm redistribution rights before piping vendor data into the channel.

### Ops & cost
- **Cost & rate-limit guardrails.** <60s polling × N accounts × per-request pricing + LLM tokens can balloon. Need a hard monthly budget cap, exponential backoff, and **per-account polling cadence** (poll the sharp accounts faster).
- **Feed-death observability.** Third-party X scraping is ToS-gray and breaks without warning. Add a **dead-man's-switch alert** ("no posts ingested in N min"), a fallback provider, and an LLM-cost + outcome dashboard.
- **Dedup & alert fatigue.** Same ticker posted 50×, daily "gm $SPY," retweets, deleted/edited posts. Dedupe + batch to keep the channel signal-dense.

---

## Risk Flags (your aggressive config)

You selected the highest-risk option at three forks: all assets at once, auto-broadcast (no human gate), and live from day one. That's your call — registered here for the record. The safety rails above are the agreed mitigation; the v1 feedback loop still runs so you accumulate a track record even though you aren't gating on it. Recommend revisiting the broadcast gate after the first month of measured outcomes.

---

## Open Questions (resolve during `/ce:plan` — mostly HOW)

1. **Which third-party X provider** (twitterapi.io vs apify vs other) — compare real-time latency, per-request cost, and reliability.
2. **Market-data vendors** — likely Polygon.io (equities + options), Financial Modeling Prep (fundamentals), CoinGecko/exchange APIs (crypto). Confirm coverage + redistribution terms.
3. **LLM routing** — which model(s) for extraction vs. deep analysis; cost/latency budget per stage.
4. **Hosting** — always-on worker target (Fly.io / Railway / VPS); Vercel only if/when a dashboard is added (cron floors at 1 min, so the <60s loop cannot live there).
5. **Watchlist specifics** — seed account list + per-account polling cadence tiers.

---

## Resolved Questions

All 14 decisions in the table above were resolved during this brainstorm (audience, data access, recommendation posture, budget, asset universe, latency, feedback loop, chart output, architecture, stack, rollout, broadcast gate, go-live, seed accounts).
