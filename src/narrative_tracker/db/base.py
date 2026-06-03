"""Async engine / session factory.

Engine and sessionmaker are built explicitly (dependency injection) rather than
hidden in globals, so tests construct an in-memory/temp SQLite database and prod
constructs Postgres from settings — same code path.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine for the given SQLAlchemy URL."""
    return create_async_engine(database_url, echo=echo, future=True)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create all tables (dev/test convenience; prod uses migrations later)."""
    # Import models so they register on Base.metadata.
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
