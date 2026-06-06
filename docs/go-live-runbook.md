# Go-Live Runbook

Concrete steps to take the Narrative Tracker from "tests pass" to "running in
production." **Default posture is paper-trade** — the system generates and scores
calls without broadcasting until you flip it.

> ⚠️ **Before you broadcast to a group:** auto-broadcasting buy/sell calls to people
> who act on them can constitute investment advice and carries liability. Paper-trade
> first (Step 6), and get a professional compliance check before scaling the group.
> This runbook is engineering guidance, not legal/financial advice.

---

## 1. Create accounts + get keys

| Service | Why | Plan / cost |
|---|---|---|
| **twitterapi.io** | X feed (ingestion) | stream ~$149/mo · *required* |
| **Telegram** | bot + 2 channels (trading + ops) | free · *required* |
| **Postgres** | system of record | Neon/Supabase ~$0–25/mo · *required* |
| **Polygon ("Massive")** | prices/bars → recommend + scoring | ~$79–158/mo · *optional, enables calls* |
| **LLM provider** | stance/vision (else rule-based) | usage, capped · *optional* |
| **Redis (Upstash)** | dedupe/budget cache | free–$5 · optional |
| **Healthchecks.io / Sentry** | dead-man's switch / errors | free · recommended |

Telegram bot: talk to **@BotFather** → `/newbot` → get the token. Create two
channels, add the bot as **admin**, and get the numeric chat ids (e.g. forward a
message to **@userinfobot** or use the Bot API `getUpdates`).

## 2. Configure

```bash
cp .env.example .env
# Fill at minimum:
#   NT_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/narrative_tracker
#   NT_TWITTERAPI_IO_KEY=...
#   NT_TELEGRAM_BOT_TOKEN=...
#   NT_TELEGRAM_TRADING_CHAT_ID=-100...
#   NT_TELEGRAM_OPS_CHAT_ID=-100...
# Enable calls (optional): NT_POLYGON_API_KEY=...
# Better stance (optional):  NT_LLM_MODEL=anthropic/claude-opus-4-8  (+ provider key)
# SAFETY: leave NT_PAPER_TRADE=true for now.
```

## 3. Preflight

```bash
pip install -e ".[dev,prod]"
python -m narrative_tracker.preflight     # prints a GO / NO-GO checklist
```
Fix any `[XX]` required items before continuing.

## 4. Deploy

**Option A — single box (docker-compose):**
```bash
docker compose up -d --build       # postgres + redis + worker + api
docker compose logs -f worker
curl localhost:8000/health
```

**Option B — managed (Railway/Fly/Hetzner):** deploy the `worker` process (and
optionally `web` = the health API, see `Procfile`); point `NT_DATABASE_URL` /
`NT_REDIS_URL` at managed instances. The worker runs `create_all` on first boot
(swap to Alembic migrations before heavy production use).

## 5. Seed the watchlist

In the bot's **admin DM** (your Telegram user id must be in the admin allowlist):
```
/addsource <handle> tier=HOT      # the sharp accounts
/addsource <handle> tier=WARM
/sources                          # verify
```

## 6. Paper-trade (do not skip)

With `NT_PAPER_TRADE=true`, for ~2–4 weeks:
- Watch the **digests** land in the trading channel (these DO post — they're analysis, not calls).
- The system generates calls, **scores them**, and updates account credibility — **without broadcasting**.
- Review the track record: are the calls profitable? Is the credibility leaderboard sensible?
- Tune the Conservative thresholds (`RiskConfig`) against what you see.

## 7. Flip to live

Only once the paper record clears your bar:
```bash
# set NT_PAPER_TRADE=false  (and redeploy / restart the worker)
python -m narrative_tracker.preflight   # confirms LIVE mode + a warning
```
Now passed calls broadcast to the trading channel.

## 8. Monitor

- **Ops channel** — heartbeat, budget %, pump suppressions, feed failover.
- **Healthchecks.io** — pages you if ingestion stalls (dead-man's switch).
- **Sentry** — worker/API exceptions.
- **Budget** — LiteLLM cap + the data-API ledger; watch the burn-down.

## 9. Kill-switch drill (do this before trusting it)

```
/pause broadcast     # hold outbound calls, keep ingesting + analyzing
/resume
/kill                # halt everything (double-confirm; survives restart)
```
Confirm each works from the admin DM. The kill switch is Postgres-authoritative
and fails closed.

## 10. Rollback

- `/kill` (immediate halt), or stop the worker process / `docker compose stop worker`.
- Calls already broadcast can be retracted (the bot replies "INVALIDATED" to the
  original message); deleting the source post auto-triggers a retraction.

## 11. Compliance checklist (before scaling beyond yourself)

- [ ] Disclaimer present on every call (enforced in the send path — it is).
- [ ] Immutable audit log retained (it is).
- [ ] Professional legal/compliance review of broadcasting calls to a group.
- [ ] Market-data vendor redistribution terms respected (post derived analysis, not raw data).
