"""FastAPI health surface (prod-only).

``GET /health`` reports liveness plus the freshness of ingestion (max
``posts.ingested_at``) so an operator/monitor can see if the feed has gone quiet.
The dead-man's-switch heartbeat (ops.heartbeat) is the active alarm; this is the
pull-based view.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import func, select

from ..config import get_settings
from ..db.base import build_engine, build_sessionmaker
from ..db.models import Post

app = FastAPI(title="Narrative Tracker", version="0.0.1")

_engine = build_engine(get_settings().database_url)
_session_factory = build_sessionmaker(_engine)


@app.get("/health/live")
async def live() -> dict:
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    async with _session_factory() as session:
        last = await session.scalar(select(func.max(Post.ingested_at)))
        count = await session.scalar(select(func.count(Post.id)))
    return {
        "status": "ok",
        "posts_ingested": int(count or 0),
        "last_ingest_at": last.isoformat() if last else None,
    }
