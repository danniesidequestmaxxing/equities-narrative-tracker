# Narrative Tracker

Watches curated X/Twitter accounts, detects ticker mentions (equities + options + crypto), and delivers auditable trade signals to Telegram — with a self-scoring feedback loop that re-weights each account by realized accuracy.

- **Brainstorm:** [`docs/brainstorms/2026-06-03-equities-narrative-tracker-brainstorm.md`](docs/brainstorms/2026-06-03-equities-narrative-tracker-brainstorm.md)
- **Plan (HOW):** [`docs/plans/2026-06-03-feat-equities-narrative-tracker-plan.md`](docs/plans/2026-06-03-feat-equities-narrative-tracker-plan.md)
- **Design specs:** [`docs/design/`](docs/design/)

## Status: M0 — pipeline spine

The current milestone proves the spine end-to-end:

```
X push  →  ingest (own process, dedupe)  →  cashtag extract  →  idempotent Telegram alert  (<60s)
```

Core invariants live from line one:
- **INV-1** — Postgres is the authority; the `sent_messages` claim is an INSERT against a unique constraint **before** the Telegram send.
- **INV-4** — every side-effecting step is idempotent (a worker restart never double-posts).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + test deps (no credentials needed)
pytest                            # runs against in-memory SQLite

# To run against real services, also install the prod extra and set .env:
pip install -e ".[dev,prod]"
cp .env.example .env             # fill in twitterapi.io + Telegram + Postgres
```

## Layout

```
src/narrative_tracker/
├── config.py              # pydantic-settings, stub-safe defaults
├── db/                    # async SQLAlchemy models, engine, idempotency ledger
├── extract/cashtag.py     # cashtag → mention (M0)
├── ingest/                # provider protocol, twitterapi.io stream client, buffer
├── notify/                # MarkdownV2 escaping + Telegram alert (claim-before-send)
├── api/health.py          # FastAPI /health + heartbeat
└── worker.py              # the spine
```

Not investment advice. See the plan's compliance notes before broadcasting to anyone.
