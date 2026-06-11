"""8-hour Pulse: the investor briefing (M8).

Every N hours the worker reads the **durable** window of tracked-account posts
(ticker_mentions ⋈ posts ⋈ accounts — restart-proof, unlike the in-memory
Analyzer) and broadcasts one Telegram message answering three questions:

1. *What was posted?*           — per-account recap + quiet accounts
2. *What's the market on about?* — hot tickers w/ momentum vs the prior window
3. *Which narratives matter?*    — LLM-written brief (seed-theme fallback)

Plus the investor edge: an **early radar** (first-appearance / accelerating
tickers) and a **chart & fundamentals check** (TA snapshot + market cap/sector)
on the top names when Polygon is configured. Pure functions here; IO lives in
``jobs.run_pulse``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from ..notify.escaping import md, md_code
from .narratives import assign_narratives

_STANCE_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0, "unclear": 0}


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _sent_emoji(s: float) -> str:
    if s > 0.15:
        return "\U0001f7e2"  # 🟢
    if s < -0.15:
        return "\U0001f534"  # 🔴
    return "\U0001f7e1"  # 🟡


# ---------------------------------------------------------------- early radar

def early_radar(
    window_rows: list[dict],
    prior_rows: list[dict],
    *,
    window_hours: float,
    prior_hours: float = 168.0,
    accel_factor: float = 3.0,
    max_items: int = 3,
) -> list[dict]:
    """Flag tickers worth early research: first appearance in a week, or a
    mention rate >= ``accel_factor``x their trailing baseline. Multi-account
    chatter ranks above one account talking to itself."""
    win: dict[str, list[dict]] = defaultdict(list)
    for r in window_rows:
        win[r["symbol"]].append(r)
    prior_counts: dict[str, int] = defaultdict(int)
    for r in prior_rows:
        prior_counts[r["symbol"]] += 1

    out = []
    for sym, group in win.items():
        mentions = len(group)
        accounts = sorted({g["handle"] for g in group})
        prior = prior_counts.get(sym, 0)
        if prior == 0:
            kind = "new"
        else:
            rate_now = mentions / window_hours
            rate_prior = prior / prior_hours
            if rate_now < accel_factor * rate_prior or mentions < 2:
                continue
            kind = "accelerating"
        out.append({
            "symbol": sym, "kind": kind, "mentions": mentions,
            "accounts": len(accounts), "handles": accounts[:3],
        })
    # multi-account first; then by raw mentions
    out.sort(key=lambda x: (x["accounts"], x["mentions"]), reverse=True)
    return out[:max_items]


# ------------------------------------------------------------- account recap

def account_recap(window_rows: list[dict], *, max_accounts: int = 6, max_symbols: int = 4) -> list[dict]:
    """Per-account: posts written + the tickers they hit, with a net-stance emoji."""
    by_handle: dict[str, list[dict]] = defaultdict(list)
    for r in window_rows:
        by_handle[r["handle"]].append(r)

    recap = []
    for handle, group in by_handle.items():
        posts = len({g["platform_post_id"] for g in group})
        per_sym: dict[str, int] = defaultdict(int)
        net: dict[str, int] = defaultdict(int)
        for g in group:
            per_sym[g["symbol"]] += 1
            net[g["symbol"]] += _STANCE_SIGN.get(g["stance"], 0)
        symbols = [
            {"symbol": s, "emoji": _sent_emoji(0.2 if net[s] > 0 else -0.2 if net[s] < 0 else 0.0)}
            for s, _ in sorted(per_sym.items(), key=lambda kv: -kv[1])[:max_symbols]
        ]
        recap.append({"handle": handle, "posts": posts, "symbols": symbols})
    recap.sort(key=lambda x: -x["posts"])
    return recap[:max_accounts]


# -------------------------------------------------- narrative fallback (no LLM)

def fallback_narratives(window_rows: list[dict], *, max_items: int = 4) -> list[dict]:
    """Seed-theme aggregation over the window — used when no LLM is configured."""
    agg: dict[str, dict] = {}
    for r in window_rows:
        for label in assign_narratives(r["symbol"], r["text"] or ""):
            a = agg.setdefault(label, {"count": 0, "net": 0, "tickers": set()})
            a["count"] += 1
            a["net"] += _STANCE_SIGN.get(r["stance"], 0)
            a["tickers"].add(r["symbol"])
    out = [
        {"title": label, "tickers": sorted(a["tickers"])[:5],
         "takeaway": f"{a['count']} mentions, net {'bullish' if a['net'] > 0 else 'bearish' if a['net'] < 0 else 'mixed'} lean."}
        for label, a in sorted(agg.items(), key=lambda kv: -kv[1]["count"])
    ]
    return out[:max_items]


# ------------------------------------------------------------------ LLM brief

PULSE_SYSTEM_PROMPT = """\
You are the narrative analyst inside an equities tracker. You receive posts that
tracked finance/crypto X accounts wrote over the last few hours, plus per-ticker
stats (mentions, distinct accounts, credibility-weighted sentiment) and flags for
newly-emerging tickers.

Write an investor briefing:
1. headline — ONE line: the single most important thing that happened this window.
2. narratives — 2-4 DISTINCT market narratives (themes, not per-ticker rehash).
   For each: a short title, the tickers expressing it, and a <=2 sentence takeaway
   that says what it means and what to watch next (catalyst, level, confirmation).
3. early_radar — one or two sentences on which emerging/first-appearance names
   deserve early research and why; empty string if none qualify.

Rules: analytical tone, no hype, no financial advice, never mention tickers that
are not in the data, keep takeaways concrete."""


class NarrativeNote(BaseModel):
    title: str
    takeaway: str = Field(description="<=2 sentences: what it means + what to watch next")
    tickers: list[str] = Field(default_factory=list)


class PulseBrief(BaseModel):
    headline: str
    narratives: list[NarrativeNote] = Field(default_factory=list)
    early_radar: str = ""


PulseWriter = Callable[[str], Awaitable[PulseBrief]]


def build_writer_context(
    window_rows: list[dict], hot: list[dict], early: list[dict], *, hours: float, max_posts: int = 40
) -> str:
    """Compact, capped context for the LLM writer: stats first, then the posts
    (HOT-tier accounts first, then recency; texts truncated)."""
    lines = [f"WINDOW: last {hours:g} hours"]
    lines.append("TICKER STATS (mentions/accounts/sentiment):")
    for t in hot[:10]:
        lines.append(f"  ${t['symbol']}: {t['mentions']}m/{t['accounts']}a S{t['sentiment']:+.2f}")
    if early:
        lines.append("EMERGING: " + ", ".join(f"${e['symbol']} ({e['kind']})" for e in early))
    lines.append("POSTS:")
    tier_rank = {"HOT": 0, "WARM": 1, "COLD": 2}
    ordered = sorted(
        window_rows,
        key=lambda r: (tier_rank.get(r["tier"], 3), -_utc(r["posted_at"]).timestamp()),
    )
    seen: set[str] = set()
    for r in ordered:
        if r["platform_post_id"] in seen:
            continue  # one line per post even when it mentions several tickers
        seen.add(r["platform_post_id"])
        text = " ".join((r["text"] or "").split())[:220]
        lines.append(f"  @{r['handle']} [{r['tier']}]: {text}")
        if len(seen) >= max_posts:
            break
    return "\n".join(lines)


def build_pulse_writer(model: str | None) -> PulseWriter | None:  # pragma: no cover
    """LLM pulse writer via instructor (same provider plumbing as stance).
    None when no model is configured -> seed-theme fallback."""
    if not model:
        return None

    async def write(context: str) -> PulseBrief:
        import instructor  # lazy: part of the `prod` extra

        client = instructor.from_provider(model, async_client=True)
        return await client.chat.completions.create(
            response_model=PulseBrief,
            max_retries=2,
            max_tokens=2048,  # headline + 2-4 narratives; Anthropic requires max_tokens
            messages=[
                {"role": "system", "content": PULSE_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
        )

    return write


# ------------------------------------------------------------------ formatting

def _mcap(v: float) -> str:
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= div:
            return f"${v / div:.1f}{suf}"
    return f"${v:,.0f}" if v else "—"


def _fmt_px(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}" if v < 1000 else f"{v:,.0f}"


_DELTA_EMOJI = {"new": "\U0001f195", "up": "↑", "down": "↓", "flat": "·"}  # 🆕


def build_pulse(
    *,
    window_label: str,
    date_label: str,
    posts_count: int,
    accounts_count: int,
    tickers_count: int,
    hot: list[dict],            # analytics.hot_tickers rows + "delta" key
    brief: PulseBrief | None,   # LLM brief (None -> fallback)
    narratives: list[dict],     # fallback narrative dicts (used when brief is None)
    early: list[dict],
    deep_dives: list[dict],     # {symbol, ta: snapshot dict, name, market_cap, sector}
    recap: list[dict],
    quiet: list[str],
    market_hint: bool = False,  # True -> tell the user how to enable TA section
    llm_hint: bool = False,
) -> tuple[str, str]:
    """Render the pulse as ``(markdown_v2, plain)``. Sections are capped so the
    message stays under Telegram's 4096-char limit."""
    L: list[str] = [
        f"\U0001f558 *{md(window_label + ' Pulse')}* — {md(date_label)}",
        f"_{posts_count} posts · {accounts_count} accounts · {tickers_count} tickers_",
    ]

    # -- narratives
    L += ["", "*\U0001f9ed Narratives to notice*"]
    notes = (
        [{"title": n.title, "tickers": n.tickers, "takeaway": n.takeaway} for n in brief.narratives[:4]]
        if brief else narratives[:4]
    )
    if brief and brief.headline:
        L.insert(2, f"\n\U0001f4cc _{md(brief.headline)}_")
    if notes:
        for i, n in enumerate(notes, 1):
            tick = " ".join("$" + t.lstrip("$") for t in n["tickers"][:5])
            L.append(f"{i}\\. *{md(n['title'])}*" + (f" — {md(tick)}" if tick else ""))
            if n.get("takeaway"):
                L.append(f"   ↳ _{md(str(n['takeaway'])[:240])}_")
    else:
        L.append("_no clustered narratives this window_")
    if llm_hint:
        L.append("_\\(set NT\\_LLM\\_MODEL for written analysis\\)_")

    # -- hot tickers
    L += ["", "*\U0001f525 What the market's talking about*"]
    if hot:
        for t in hot[:8]:
            d = _DELTA_EMOJI.get(t.get("delta", "flat"), "·")
            s_txt = md(f"{t['sentiment']:+.2f}")
            L.append(
                f"{_sent_emoji(t['sentiment'])} `{md_code('$' + t['symbol'])}` "
                f"{t['mentions']}×/{t['accounts']}acct · S {s_txt} {d}"
            )
    else:
        L.append("_no mentions this window_")

    # -- early radar
    L += ["", "*\U0001f4e1 Early radar*"]
    if brief and brief.early_radar:
        L.append(f"_{md(brief.early_radar[:300])}_")
    elif early:
        for e in early:
            why = "first appearance in 7d" if e["kind"] == "new" else "mention rate accelerating"
            who = ", ".join("@" + h for h in e["handles"])
            L.append(f"\U0001f195 `{md_code('$' + e['symbol'])}` — {md(why)} · {e['accounts']}acct \\({md(who)}\\)")
    else:
        L.append("_nothing newly emerging_")

    # -- deep dive
    L += ["", "*\U0001f52c Chart \\& fundamentals check*"]
    if deep_dives:
        for d in deep_dives:
            ta = d["ta"]
            bits = [f"trend {ta['trend']}"]
            if ta.get("rsi") is not None:
                bits.append(f"RSI {ta['rsi']:.0f}")
            if ta.get("chg_5d") is not None:
                bits.append(f"5d {ta['chg_5d']:+.1f}%")
            if ta.get("vol_ratio") is not None:
                bits.append(f"vol {ta['vol_ratio']:.1f}x")
            if ta.get("off_high_pct") is not None:
                bits.append(f"{ta['off_high_pct']:.0f}% off 52w high")
            bell = "\U0001f514 " if d.get("watched") else ""  # 🔔 user-watched
            L.append(f"{bell}`{md_code('$' + d['symbol'])}` {md(_fmt_px(ta['price']))} · {md(' · '.join(bits))}")
            fund = [x for x in (d.get("sector"), _mcap(d.get("market_cap") or 0.0)) if x and x != "—"]
            levels = f"sup {_fmt_px(ta['support'])} / res {_fmt_px(ta['resistance'])}"
            L.append(f"   _{md(' · '.join(fund + [levels]))}_")
    elif market_hint:
        L.append("_set NT\\_POLYGON\\_API\\_KEY for TA \\+ fundamentals here_")
    else:
        L.append("_no chartable equities this window_")

    # -- account recap
    L += ["", "*\U0001f465 Your accounts*"]
    if recap:
        for a in recap:
            syms = " ".join(f"${s['symbol']}{s['emoji']}" for s in a["symbols"])
            L.append(f"@{md(a['handle'])} — {a['posts']} post{'s' if a['posts'] != 1 else ''} · {md(syms)}")
    else:
        L.append("_no posts from tracked accounts_")
    if quiet:
        shown = ", ".join("@" + h for h in quiet[:8])
        more = f" \\+{len(quiet) - 8} more" if len(quiet) > 8 else ""
        L.append(f"_quiet: {md(shown)}{more}_")

    L += ["", "_Analysis only · derived metrics · not financial advice_"]
    mdv2 = _cap_lines(L)

    plain = "\n".join([
        f"[{window_label} PULSE] {date_label}",
        f"{posts_count} posts / {accounts_count} accounts / {tickers_count} tickers",
        ("Headline: " + brief.headline) if brief and brief.headline else "",
        "Narratives: " + "; ".join(f"{n['title']} ({', '.join(n['tickers'][:4])})" for n in notes) if notes else "Narratives: none",
        "Hot: " + ", ".join(f"${t['symbol']} {t['mentions']}x S{t['sentiment']:+.2f}" for t in hot[:8]),
        "Early: " + (", ".join(f"${e['symbol']} ({e['kind']})" for e in early) if early else "none"),
        "Analysis only - not financial advice",
    ])
    return mdv2, plain


def _cap_lines(lines: list[str], limit: int = 3900) -> str:
    """Join lines; if over Telegram's limit, drop whole lines from the tail of the
    longest sections (line-bounded so MarkdownV2 entities stay balanced)."""
    text = "\n".join(lines)
    while len(text) > limit and len(lines) > 10:
        # drop the last non-footer content line (footer = final 2 lines)
        del lines[-3]
        text = "\n".join(lines)
    return text
