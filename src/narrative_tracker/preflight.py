"""Go-live preflight: validate config + connectivity, print a go/no-go.

    python -m narrative_tracker.preflight
"""

from __future__ import annotations

import asyncio

from .config import Settings, get_settings

# The minimum needed to run *anything* useful.
REQUIRED = {"x_feed", "telegram"}


def check_readiness(settings: Settings) -> list[tuple[str, bool, str]]:
    """Pure config readiness check -> list of (component, ok, detail)."""
    scheme = settings.database_url.split("://", 1)[0]
    return [
        ("database", bool(settings.database_url), f"{scheme}://…"),
        ("x_feed", bool(settings.twitterapi_io_key), "set" if settings.twitterapi_io_key else "MISSING — no ingestion"),
        ("telegram", settings.telegram_configured, "set" if settings.telegram_configured else "MISSING — no alerts/calls"),
        ("market_data", bool(settings.polygon_api_key), "set — recommend+scoring enabled" if settings.polygon_api_key else "missing — recommend+scoring DISABLED"),
        ("llm_stance", bool(settings.llm_model), settings.llm_model or "rule-based fallback (neutral on momentum tweets)"),
        ("ops_channel", bool(settings.telegram_ops_chat_id), "set" if settings.telegram_ops_chat_id else "missing — health to logs only"),
        ("heartbeat", bool(settings.healthchecks_url), "set" if settings.healthchecks_url else "missing — no dead-man's switch"),
        ("mode", True, "PAPER (no broadcast)" if settings.paper_trade else "LIVE — broadcasting calls!"),
    ]


def is_go(checks: list[tuple[str, bool, str]]) -> bool:
    return not any((not ok) and name in REQUIRED for name, ok, _ in checks)


async def check_db(settings: Settings) -> tuple[bool, str]:
    try:
        from .db.base import build_engine

        engine = build_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True, "connected"
    except Exception as exc:  # noqa: BLE001
        return False, f"connect failed: {exc}"


async def main() -> None:  # pragma: no cover - CLI
    settings = get_settings()
    checks = check_readiness(settings)
    db_ok, db_detail = await check_db(settings)
    checks = [
        (name, db_ok if name == "database" else ok, db_detail if name == "database" else detail)
        for name, ok, detail in checks
    ]
    print("Narrative Tracker — preflight\n" + "=" * 44)
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'XX'}] {name:13} {detail}")
    print("=" * 44)
    go = is_go(checks) and db_ok
    print("RESULT:", "GO" if go else "NO-GO  (fix the [XX] required items)")
    if not settings.paper_trade:
        print("\n!!  LIVE mode: calls WILL be broadcast to the group.")
        print("    Confirm you have paper-traded and accept the responsibility.")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
