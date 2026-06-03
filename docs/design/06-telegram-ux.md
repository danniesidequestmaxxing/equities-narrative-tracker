---
title: "Design: Telegram UX + Message Templates"
status: design-detail
date: 2026-06-03
parent_plan: ../plans/2026-06-03-feat-equities-narrative-tracker-plan.md
milestone: M0/M5
modules: [notify/escaping.py, notify/templates.py, notify/sender.py, notify/coalescer.py, admin/commands.py]
---

# Telegram UX + Message Templates (2026) — aiogram 3.x

Two channels: **TRADING** (`CHAN_TRADING`, alerts/digests/calls) and **OPS** (`CHAN_OPS`, health/budget/errors). All sends through one `SendQueue` per chat. `parse_mode=MarkdownV2`. Limits (2026): 4096-char text, 1024-char caption, ~1 msg/s per chat, ~20 msg/min to a group/channel.

## 0. MarkdownV2 escaping (foundation)
**18 reserved chars to escape in any text position:** `_ * [ ] ( ) ~ ` > # + - = | { } . !`. Inside `` `code` ``: only `` ` `` and `\`. Inside a `[text](URL)` destination: only `)` and `\`. An unescaped reserved char → `400 can't parse entities` → **the whole message is dropped** (= a missed call). The escaper is non-negotiable.
```python
# escaping.py
_MDV2_SPECIAL = r'_*[]()~`>#+-=|{}.!'
_MDV2_TRANS = str.maketrans({c: '\\'+c for c in _MDV2_SPECIAL})
def md(text)      -> str: return str(text).translate(_MDV2_TRANS)          # text position
def md_code(text) -> str: return str(text).replace('\\','\\\\').replace('`','\\`')   # inside ` `
def md_url(url)   -> str: return str(url).replace('\\','\\\\').replace(')','\\)')    # link dest
```
`str(...)` coercion means a `Decimal`/`None`/`BRK.B`/`@deep_value_dan` never crashes formatting. **Render tickers in code-spans** (`` `$BRK.B` ``) — only backtick/backslash to escape, visually clean, parse-safe.

**Never-drop-a-call guarantee** (every builder returns both `mdv2` and `plain`):
```python
async def safe_send(bot, chat_id, text_mdv2, plain_fallback, **kw):
    try: return await bot.send_message(chat_id, text_mdv2, parse_mode="MarkdownV2", **kw)
    except TelegramBadRequest as e:
        if "can't parse entities" in str(e).lower():
            log.error("MDV2 parse fail, sending plain")
            return await bot.send_message(chat_id, plain_fallback, parse_mode=None, **kw)
        raise
```

## 1. Templates
Visual system: 🟢 bullish / 🔴 bearish / 🟡 mixed · ⚡ alert · 📊 digest · 🎯 call · ↩️ retraction · 🛠 ops. Confidence as `0.82` + 5-block bar `▰▰▰▰▱`. Times `HH:MM ET`.

### 1.1 Real-time ALERT
```
⚡ *$NVDA* · 🟢 Bullish · `0.82` ▰▰▰▰▱
[@deep_value_dan](url) just posted on $NVDA
Tier A source · 14:23 ET · 30s ago
[📈 Chart](tradingview_url) · #NVDA #alert
_Derived signal · not financial advice_
```

### 1.2 Daily/Weekly DIGEST (ToS-critical)
Must be **derived analysis** — narrative themes, momentum deltas, credibility-weighted aggregates *you* compute. Never paste raw vendor rows or reproduce a source post verbatim. Sections: header, top narratives (with momentum %), credibility-weighted hot tickers, momentum movers (σ), footer.

**Safe <4096 pagination** (never split mid-line or inside an entity; footer becomes `1/3, 2/3, 3/3`):
```python
DIGEST_SOFT_LIMIT = 3900   # headroom for footer
def paginate_sections(sections, footer_fmt):   # sections = complete entity-balanced MDV2 blocks
    pages, cur = [], ""
    def flush():
        nonlocal cur
        if cur: pages.append(cur); cur=""
    for sec in sections:
        block = sec if not cur else cur+"\n\n"+sec
        if len(block) <= DIGEST_SOFT_LIMIT: cur=block; continue
        flush()
        if len(sec) <= DIGEST_SOFT_LIMIT: cur=sec
        else:
            for line in sec.split("\n"):
                if len(cur)+len(line)+1 > DIGEST_SOFT_LIMIT: flush()
                cur = line if not cur else cur+"\n"+line
    flush(); n=len(pages)
    return [p+"\n\n"+footer_fmt.format(i=i+1,n=n) for i,p in enumerate(pages)]
```
Hot-ticker rows use code-span tickers (`` `$NVDA` ``) → `BRK.B` is parse-safe. Pages enqueued as an atomic ordered burst.

### 1.3 Explicit CALL (all required fields)
```
🎯 *CALL* · *$NVDA* · 🟢 *LONG*
▰▰▰▰▱ confidence `0.82`
*Entry* `148.00 – 150.50`
*Stop* `141.20` (-5.6%)
*Targets* `T1 162.00` · `T2 175.00`
*Size* `1.5% NAV` · *Horizon* `swing · 3–10d`
*R/R* `≈ 2.4 : 1`
*Driving accounts*
• [@deep_value_dan](url) (A)
• [@semi_scoop](url) (A)
• +3 Tier-B corroborating
⚠️ *Catalyst watch* GTC keynote 04 Jun · earnings 28 Aug
⚠️ Event risk inside horizon — size accordingly
[📈 Open in TradingView](url)
_NOT FINANCIAL ADVICE. For information only. You are responsible for your own trades. The bot may hold or act on these names._
`CALL-2026-0603-NVDA-017`
```
`call_id` = the idempotency-ledger key AND the retraction reply target. Targets built from a **shared `targets` Pydantic schema** reused by gates + scorer (no drift).

### 1.4 RETRACTION / INVALIDATION (replies to original)
Triggers: (a) source post deleted → retract derived alerts; (b) call invalidated (stop hit / thesis broken). Replies-to the original message (persist `message_id` in the ledger at send time):
```python
return await safe_send(bot, ref.chat_id, mdv2, plain,
    reply_parameters=ReplyParameters(message_id=ref.message_id, allow_sending_without_reply=True))
```
`allow_sending_without_reply=True` → ships even if the original was deleted. Retractions are **priority-class CRITICAL** — bypass coalescing, jump ahead of alerts (a stale un-retracted bullish call is the worst failure).

## 3. Admin command set (DM-only, gated)
```python
ADMIN_IDS = {111111111, 222222222}
class IsAdmin(BaseFilter):
    async def __call__(self, msg): return msg.from_user and msg.from_user.id in ADMIN_IDS
admin_router = Router(); admin_router.message.filter(IsAdmin)
admin_router.callback_query.filter(lambda cq: cq.from_user.id in ADMIN_IDS)
```
| Command | Args | Class | Effect |
|---|---|---|---|
| `/addsource` | `<handle> [tier=A\|B\|C]` | mutating | add source (default C) |
| `/rmsource` | `<handle>` | **destructive** | stop tracking (confirm) |
| `/filter` | `<handle> [+/-ticker…] [min_conf=]` | mutating | per-source include/exclude + conf floor |
| `/tier` | `<handle> <A\|B\|C>` | mutating | set credibility tier |
| `/cadence` | `<digest\|alerts> <value>` | mutating | `daily 16:30 ET`, `weekly Fri 17:00`, `alerts realtime\|batched=5m` |
| `/pause` | `[broadcast\|full] [dur]` | destructive(full) | two-mode pause |
| `/resume` | — | mutating | clear pause |
| `/kill` | — | **destructive** | halt ingest+send+flush queue (double-confirm, persisted flag) |
| `/status` | `[queue\|sources\|budget]` | read | snapshot |
| `/sources` `/testalert` | — / `<ticker>` | read/dry | list / render sample to admin DM |

**Two-mode pause:** `broadcast` = keep ingest+analysis+ledger, hold outbound TRADING (OPS flows); resume → coalesce-then-release (no flood). `full` = pause ingest too (destructive). `/kill` = hardest stop + persisted `KILLED` flag surviving restart.

**Nonce-bound confirm (60s TTL) for destructive actions:**
```python
def confirm_kb(action, token):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"cfm:{action}:{token}"),
        InlineKeyboardButton(text="✖️ Cancel",  callback_data=f"cxl:{action}:{token}")]])
PENDING = {}   # token -> {action, args, expires}
@admin_router.callback_query(F.data.startswith("cfm:"))
async def on_confirm(cq):
    _, action, token = cq.data.split(":",2); p = PENDING.pop(token, None)
    if not p or p["expires"] < time.time(): return await cq.answer("Expired or used.", show_alert=True)
    ...  # perform action; edit card
```
`/kill` uses double-confirm. Nonce + TTL → a stale card in scrollback can't re-trigger.

## 4. Rate limiting + idempotency
One token-bucket SendQueue per chat (sized to the lower limit + reactive 429 back-off). Centralizing sends makes rate-limiting and idempotency enforceable in one place.
```python
class ChatSender:
    def __init__(self, bot, chat_id):
        self.min_interval=1.05; self.minute_cap=18   # under 20/min
        self.q=asyncio.PriorityQueue(); self._sent_ts=deque()
    async def enqueue(self, msg):
        if msg.dedup_key and await ledger.already_sent(msg.dedup_key): return   # dedup at enqueue
        await self.q.put(msg)
    async def run(self):
        while True:
            msg=await self.q.get(); await self._respect_limits()
            try:
                mdv2,plain,kw = msg.build()
                sent=await safe_send(self.bot,self.chat_id,mdv2,plain,**kw)
                if msg.dedup_key: await ledger.mark_sent(msg.dedup_key, chat_id=self.chat_id, message_id=sent.message_id)
                self._note_send()
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after+0.5); await self.q.put(msg)   # requeue, DO NOT mark sent
            finally: self.q.task_done()
```
**Ledger interaction:** dedup at enqueue (`dedup_key` = `ALERT-<date>-<symbol>-<source_msg_id>` / `CALL-…` / `DIGEST-<date>-<page>`); `mark_sent` only on **success** (a 429 retry never self-dedupes) and stores `message_id` for retractions. Make `already_sent`+`mark_sent` one atomic upsert (`INSERT … ON CONFLICT DO NOTHING`).

## 5. Coalescing / fatigue control
- **Per-ticker collapse:** within `W=90s`, collapse `N≥3` same-ticker alerts into one burst card (first alert fires immediately — latency matters).
- **Global cap:** `M=30 alerts/hour`; overflow accumulated → one "overflow summary" (`7 more suppressed: $AMD ×2, $MU ×3…`), never dropped.
- **Priority bypass:** CALLs + RETRACTIONs (priority 0) never coalesce, never count against M.
- **Same-direction flip suppression:** same dir + conf within ±0.05 in window → drop (ledger-recorded as represented); a direction flip always emits.
- **Quiet hours (optional):** outside RTH+ext, `W=5m`, `M=10/h`.

Every swallowed alert is `ledger.mark_represented(key, by=<lead|overflow>)` so it can't re-fire after a restart. The Coalescer is the only producer feeding `ChatSender.enqueue` → rate-limiting + idempotency apply uniformly; CALL/RETRACTION priority-0 bypass means fatigue control can never silence the must-not-miss classes.

## Key decisions
1. Render tickers in code-spans (`$BRK.B` / `$ES_F` never break parsing).
2. Escape at the boundary with a coercing `md()` (None/Decimal never crash a call).
3. `safe_send` plain-text fallback → a parse bug degrades to plain delivery, never a dropped signal.
4. CALLs + RETRACTIONs priority-0 → always ship.
5. Ledger stores `message_id` (retractions thread under original); `mark_sent` only on success (429 retry never self-dedupes).
