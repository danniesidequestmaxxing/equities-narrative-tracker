"""Admin command parsing (testable, aiogram-free).

``handle_command`` parses a message, enforces the admin allowlist, calls the
service layer, and returns the reply text. The aiogram handler (admin/bot.py)
is a thin shell around it.

Seamless input — no command syntax needed:
    @handle [hot|warm|cold]   -> track that X account
    $NVDA                     -> instant ticker brief (sentiment + takes + TA)
    $NVDA watch / unwatch     -> manage the per-ticker watchlist
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import analytics, repo, scoreboard
from . import service

HELP = (
    "Seamless input:\n"
    "@handle [hot|warm|cold] — track an X account\n"
    "$NVDA — instant ticker brief\n"
    "$NVDA watch · $NVDA unwatch — 🔔 watchlist\n"
    "\n"
    "Commands:\n"
    "/addsource <handle> [tier=HOT|WARM|COLD]\n"
    "/rmsource <handle> · /tier <handle> <TIER>\n"
    "/sources · /watch <sym> · /unwatch <sym> · /watching\n"
    "/scoreboard [days] — who's actually right\n"
    "/account <handle> [days] — one account's record\n"
    "/pause [broadcast|full] · /resume\n"
    "/kill · /unkill · /status"
)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_scoreboard(board: dict, days: int) -> str:
    lines = [f"\U0001F3C6 Scoreboard — last {days}d, edge vs SPY (3-day horizon)"]
    if not board["ranked"] and not board["thin"]:
        return ("No scored mentions yet. Outcomes are computed from daily closes, "
                "so fresh mentions need 1-5 trading days to grade. Check back tomorrow.")
    for i, a in enumerate(board["ranked"], 1):
        hit = f"{a['hit_3d'] * 100:.0f}%" if a["hit_3d"] is not None else "—"
        lines.append(
            f"{i}. @{a['handle']} ({a['tier']}) · n={a['n']} · hit {hit} · "
            f"avg {_pct(a['avg_3d'])} (1d {_pct(a['avg_1d'])} / 5d {_pct(a['avg_5d'])})"
        )
    if board["thin"]:
        thin = ", ".join(f"@{a['handle']} (n={a['n']})" for a in board["thin"])
        lines.append(f"thin sample, not ranked: {thin}")
    lines.append("Edge = benchmark-adjusted move in the direction they called. Not financial advice.")
    return "\n".join(lines)


def _fmt_account(detail: dict, days: int) -> str:
    s = detail["stats"]
    if s is None:
        return (f"No scored mentions for @{detail['handle']} in the last {days}d. "
                "Either they were quiet, or their mentions are <1 trading day old.")
    hit = f"{s['hit_3d'] * 100:.0f}%" if s["hit_3d"] is not None else "—"
    lines = [
        f"@{s['handle']} · {s['tier']} · last {days}d",
        f"scored mentions: {s['n']} · hit {hit} (3d) · "
        f"avg edge {_pct(s['avg_3d'])} (1d {_pct(s['avg_1d'])} / 5d {_pct(s['avg_5d'])})",
    ]
    if s["best"]:
        lines.append(f"best: ${s['best']['symbol']} {_pct(s['best']['edge'])} · "
                     f"worst: ${s['worst']['symbol']} {_pct(s['worst']['edge'])}")
    if detail["recent"]:
        lines.append("recent:")
        for t in detail["recent"]:
            dot = _STANCE_DOT.get(t["stance"], "⚪")
            when = t["posted_at"].strftime("%m-%d") if t["posted_at"] else ""
            edge = _pct(t["edge_3d"]) if t["edge_3d"] is not None else "pending"
            lines.append(f"{dot} ${t['symbol']} {when} → {edge}")
    if s["n"] < 5:
        lines.append("⚠️ small sample — treat as anecdote, not statistics.")
    return "\n".join(lines)

_TIERS = ("HOT", "WARM", "COLD")


async def _add_account(sf, handle: str, tier_word: str | None) -> str:
    handle = handle.lstrip("@").lower()
    if not handle:
        return "Usage: @handle [hot|warm|cold]"
    tier = tier_word.upper() if tier_word and tier_word.upper() in _TIERS else "COLD"
    await service.add_source(sf, platform_user_id=handle, handle=handle, tier=tier)
    return f"✅ Watching @{handle} ({tier}). Polling picks it up within ~2 min."


def _fmt_ta(ta: dict) -> str:
    bits = [f"trend {ta['trend']}"]
    if ta.get("rsi") is not None:
        bits.append(f"RSI {ta['rsi']:.0f}")
    if ta.get("chg_1d") is not None:
        bits.append(f"1d {ta['chg_1d']:+.1f}%")
    if ta.get("chg_5d") is not None:
        bits.append(f"5d {ta['chg_5d']:+.1f}%")
    if ta.get("vol_ratio") is not None:
        bits.append(f"vol {ta['vol_ratio']:.1f}x")
    if ta.get("off_high_pct") is not None:
        bits.append(f"{ta['off_high_pct']:.0f}% off 52w high")
    line = f"{ta['price']:,.2f} · " + " · ".join(bits)
    if ta.get("support") and ta.get("resistance"):
        line += f"\nsup {ta['support']:,.2f} / res {ta['resistance']:,.2f}"
    return line


_STANCE_DOT = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡", "unclear": "⚪"}


async def _ticker_brief(sf, symbol: str, market) -> str:
    sym = symbol.strip().lstrip("$").upper()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    d = await analytics.ticker_detail(sf, symbol=sym, since=since)
    watched = await repo.is_watched(sf, sym)

    lines = [f"${sym}{' 🔔 watching' if watched else ''}"]
    if d["mentions"]:
        lines.append(
            f"sentiment {d['sentiment']:+.2f} (n_eff {d['n_eff']}) · "
            f"{d['mentions']} mention{'s' if d['mentions'] != 1 else ''} · 24h"
        )
    else:
        lines.append("no mentions from tracked accounts in the last 24h")

    if market is not None:
        try:
            from ..analyze.technicals import snapshot_from_bars

            ta = snapshot_from_bars(await market.fetch_bars(sym, days=400, adjusted=True))
            if ta:
                lines.append(_fmt_ta(ta))
            overview = await market.fetch_overview(sym)
            fund = [x for x in (overview.get("sector"),) if x]
            mcap = overview.get("market_cap") or 0
            if mcap:
                for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
                    if mcap >= div:
                        fund.append(f"mcap ${mcap / div:.1f}{suf}")
                        break
            if fund:
                lines.append(" · ".join(fund))
        except Exception:  # noqa: BLE001 - brief still useful without TA
            lines.append("(chart data unavailable)")

    for t in d["takes"][:3]:
        dot = _STANCE_DOT.get(t["stance"], "⚪")
        txt = " ".join((t["text"] or "").split())[:90]
        lines.append(f"{dot} @{t['handle']} ({t['tier']}): {txt}")

    lines.append(f"https://www.tradingview.com/chart/?symbol={sym}")
    if not watched:
        lines.append(f"tip: '$" + sym + " watch' pins it to alerts + every pulse")
    return "\n".join(lines)


async def handle_command(text: str, from_id: int, sf, admin_ids: list[int], *, market=None) -> str:
    if not admin_ids or from_id not in admin_ids:
        return "⛔ Not authorized. (Set NT_ADMIN_IDS to your Telegram user id.)"

    parts = (text or "").strip().split()
    if not parts:
        return HELP

    from ..ops import killswitch

    try:
        # --- seamless plain-text input ---------------------------------
        if parts[0].startswith("@") and len(parts[0]) > 1:
            return await _add_account(sf, parts[0], parts[1] if len(parts) > 1 else None)

        if parts[0].startswith("$") and len(parts[0]) > 1:
            action = parts[1].lower() if len(parts) > 1 else ""
            sym = parts[0]
            if action == "watch":
                added = await repo.watch_ticker(sf, sym)
                return f"🔔 Watching ${added} — alerts get pinned and it's in every pulse deep-dive."
            if action == "unwatch":
                ok = await repo.unwatch_ticker(sf, sym)
                return f"🔕 Stopped watching ${sym.lstrip('$').upper()}." if ok else "Wasn't on the watchlist."
            return await _ticker_brief(sf, sym, market)

        cmd = parts[0].lstrip("/").lower()
        args = parts[1:]

        if cmd == "addsource":
            if not args:
                return "Usage: /addsource <handle> [tier=HOT|WARM|COLD]"
            tier = "COLD"
            for a in args[1:]:
                if a.lower().startswith("tier="):
                    tier = a.split("=", 1)[1].upper()
            return await _add_account(sf, args[0], tier)

        if cmd == "rmsource":
            if not args:
                return "Usage: /rmsource <handle>"
            handle = args[0].lstrip("@").lower()
            ok = await service.remove_source(sf, platform_user_id=handle)
            return f"🛑 Stopped watching @{handle}." if ok else f"@{handle} wasn't in the list."

        if cmd == "tier":
            if len(args) < 2:
                return "Usage: /tier <handle> <HOT|WARM|COLD>"
            handle, tier = args[0].lstrip("@").lower(), args[1].upper()
            ok = await service.set_tier(sf, platform_user_id=handle, tier=tier)
            return f"✅ @{handle} → {tier}." if ok else f"@{handle} not found."

        if cmd == "sources":
            active = [s for s in await service.list_sources(sf) if s["active"]]
            if not active:
                return "No accounts watched yet. Add one: just send @handle"
            return "Watching:\n" + "\n".join(f"• @{s['handle']} · {s['tier']}" for s in active)

        if cmd == "watch":
            if not args:
                return "Usage: /watch <symbol>  (or just send: $NVDA watch)"
            added = await repo.watch_ticker(sf, args[0])
            return f"🔔 Watching ${added} — alerts get pinned and it's in every pulse deep-dive."

        if cmd == "unwatch":
            if not args:
                return "Usage: /unwatch <symbol>"
            ok = await repo.unwatch_ticker(sf, args[0])
            return f"🔕 Stopped watching ${args[0].lstrip('$').upper()}." if ok else "Wasn't on the watchlist."

        if cmd == "watching":
            watched = await repo.watched_tickers(sf)
            if not watched:
                return "Ticker watchlist is empty. Add one: $NVDA watch"
            return "🔔 Watching: " + " ".join("$" + s for s in watched)

        if cmd == "scoreboard":
            days = int(args[0]) if args and args[0].isdigit() else 30
            board = await scoreboard.account_scoreboard(
                sf, since=datetime.now(timezone.utc) - timedelta(days=days)
            )
            return _fmt_scoreboard(board, days)

        if cmd == "account":
            if not args:
                return "Usage: /account <handle> [days]"
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
            detail = await scoreboard.account_detail(
                sf, handle=args[0], since=datetime.now(timezone.utc) - timedelta(days=days)
            )
            return _fmt_account(detail, days)

        if cmd == "pause":
            mode = killswitch.PAUSE_FULL if (args and args[0].lower() == "full") else killswitch.PAUSE_BROADCAST
            await service.pause(sf, mode)
            return f"⏸ Paused ({mode}). /resume to restore."

        if cmd == "resume":
            await service.resume(sf)
            return "▶️ Resumed."

        if cmd == "kill":
            await service.kill(sf)
            return "🛑 KILL SWITCH ENGAGED — ingestion + sending halted (survives restart). /unkill to release."

        if cmd == "unkill":
            await service.unkill(sf)
            return "✅ Kill switch released."

        if cmd == "status":
            active = [s for s in await service.list_sources(sf) if s["active"]]
            killed = await killswitch.is_killed(sf)
            pause = await killswitch.get_pause(sf)
            return f"📊 watching {len(active)} accounts · killed={killed} · pause={pause}"

        return HELP
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ error: {exc}"
