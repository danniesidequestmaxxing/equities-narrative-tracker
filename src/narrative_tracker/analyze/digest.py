"""Digest builder (M2).

Produces a Telegram-ready **derived-analysis** digest — narrative momentum and
credibility-weighted aggregates we compute, never raw vendor data (ToS). Returns
``(markdown_v2, plain)``.
"""

from __future__ import annotations

from ..notify.escaping import md, md_code

_MOM_EMOJI = {"rising": "\U0001f7e2", "peaking": "\U0001f7e1", "fading": "\U0001f534", "dormant": "⚪"}


def _sent_emoji(s: float) -> str:
    if s > 0.15:
        return "\U0001f7e2"  # 🟢
    if s < -0.15:
        return "\U0001f534"  # 🔴
    return "\U0001f7e1"  # 🟡


def build_digest(
    *,
    cadence_label: str,
    date_label: str,
    narratives: list[dict],
    hot_tickers: list[dict],
    posts_count: int = 0,
    accounts_count: int = 0,
) -> tuple[str, str]:
    lines = [
        f"\U0001f4ca *{md(cadence_label)} Narrative Digest* — {md(date_label)}",
        f"_Derived from {posts_count} tracked posts across {accounts_count} accounts_",
        "",
        "*Top narratives*",
    ]
    if narratives:
        for i, n in enumerate(narratives, 1):
            emoji = _MOM_EMOJI.get(n["momentum_state"], "⚪")
            lines.append(f"{i}\\. *{md(n['label'])}* — {emoji} {md(n['momentum_state'])}")
    else:
        lines.append("_no active narratives_")

    lines += ["", "*Credibility\\-weighted hot tickers*"]
    if hot_tickers:
        for t in hot_tickers:
            emoji = _sent_emoji(t["S"])
            s_txt = md(f"{t['S']:+.2f}")
            neff_txt = md(f"{t['n_eff']:.1f}")
            extra = ""
            if t.get("contrarian"):
                extra += f" ⚠️ {md(t['contrarian']['state'])}"
            if t.get("pump") and t["pump"].get("flag"):
                extra += f" \U0001f6a9 pump {md(t['pump']['flag'])}"
            lines.append(
                f"{emoji} `{md_code('$' + t['symbol'])}` · S {s_txt} · n\\_eff {neff_txt}{extra}"
            )
    else:
        lines.append("_no qualifying tickers_")

    lines += ["", "_Analysis only · derived metrics · not financial advice_"]
    mdv2 = "\n".join(lines)

    plain_lines = [
        f"[{cadence_label} DIGEST] {date_label}",
        f"Derived from {posts_count} posts / {accounts_count} accounts",
        "Top narratives: " + ", ".join(f"{n['label']} ({n['momentum_state']})" for n in narratives),
        "Hot tickers: "
        + ", ".join(f"${t['symbol']} S{t['S']:+.2f} n_eff{t['n_eff']:.1f}" for t in hot_tickers),
        "Analysis only - not financial advice",
    ]
    return mdv2, "\n".join(plain_lines)
