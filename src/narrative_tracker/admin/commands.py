"""Admin command parsing (testable, aiogram-free).

``handle_command`` parses a ``/command`` line, enforces the admin allowlist, calls
the service layer, and returns the reply text. The aiogram handler (admin/bot.py)
is a thin shell around it.
"""

from __future__ import annotations

from . import service

HELP = (
    "Commands:\n"
    "/addsource <handle> [tier=HOT|WARM|COLD]\n"
    "/rmsource <handle>\n"
    "/tier <handle> <HOT|WARM|COLD>\n"
    "/sources\n"
    "/pause [broadcast|full] · /resume\n"
    "/kill · /unkill · /status"
)


async def handle_command(text: str, from_id: int, sf, admin_ids: list[int]) -> str:
    if not admin_ids or from_id not in admin_ids:
        return "⛔ Not authorized. (Set NT_ADMIN_IDS to your Telegram user id.)"

    parts = (text or "").strip().split()
    if not parts:
        return HELP
    cmd = parts[0].lstrip("/").lower()
    args = parts[1:]

    from ..ops import killswitch

    try:
        if cmd == "addsource":
            if not args:
                return "Usage: /addsource <handle> [tier=HOT|WARM|COLD]"
            handle = args[0].lstrip("@").lower()
            tier = "COLD"
            for a in args[1:]:
                if a.lower().startswith("tier="):
                    tier = a.split("=", 1)[1].upper()
            await service.add_source(sf, platform_user_id=handle, handle=handle, tier=tier)
            return f"✅ Watching @{handle} ({tier}). Polling picks it up within ~2 min."

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
                return "No accounts watched yet. Add one: /addsource <handle>"
            return "Watching:\n" + "\n".join(f"• @{s['handle']} · {s['tier']}" for s in active)

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
