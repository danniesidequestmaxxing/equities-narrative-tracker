"""Options shorthand parser (M1).

Parses trader shorthand like ``$SPY 600c 0DTE``, ``$SPY c600``,
``$NVDA 200 calls Jan'27`` into an :class:`OptionDetail`. Requires a ``$`` root
to avoid matching ordinary prose ("buy 600c"). Absolute expiry-date resolution is
deferred (the raw token is preserved); M1 keeps ``expiry_raw``, ``dte_relative``,
and ``is_leaps``.

Right (call/put) must be explicit — an ambiguous "200 LEAPS" with no call/put is
NOT assumed to be a call (per the design: don't invent direction).
"""

from __future__ import annotations

import re

from ..schemas.mention import OptionDetail, OptionRight

_ROOT = r"(?<![A-Za-z0-9])\$(?P<root>[A-Za-z]{1,6})"
_REST = r"(?P<rest>[^\n,;]*)"

# 600c / 600 c   (strike then right)
_RE_STRIKE_RIGHT = re.compile(
    _ROOT + r"\s+(?P<strike>\d{1,5}(?:\.\d+)?)\s?(?P<right>[cCpP])(?![A-Za-z])" + _REST
)
# c600 / c 600   (right then strike)
_RE_RIGHT_STRIKE = re.compile(
    _ROOT + r"\s+(?P<right>[cCpP])\s?(?P<strike>\d{2,5}(?:\.\d+)?)" + _REST
)
# 600 calls / 600 puts
_RE_WORD = re.compile(
    _ROOT + r"\s+(?P<strike>\d{1,5}(?:\.\d+)?)\s+(?P<right>calls?|puts?)\b" + _REST,
    re.IGNORECASE,
)

_EXPIRY_MON = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'?\s?\d{0,4}", re.IGNORECASE
)
_EXPIRY_NUM = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_DTE = re.compile(r"\b(\d)dte\b", re.IGNORECASE)


def _parse_expiry(rest: str) -> tuple[str | None, int | None, bool]:
    rest = rest or ""
    low = rest.lower()
    is_leaps = "leap" in low
    dte_match = _DTE.search(rest)
    dte = int(dte_match.group(1)) if dte_match else None
    raw: str | None = None
    if dte_match:
        raw = dte_match.group(0)
    elif (m := _EXPIRY_MON.search(rest)) is not None:
        raw = m.group(0).strip()
    elif (m := _EXPIRY_NUM.search(rest)) is not None:
        raw = m.group(0)
    elif is_leaps:
        raw = "LEAPS"
    return raw, dte, is_leaps


def _right(token: str) -> OptionRight:
    return OptionRight.CALL if token[0].lower() == "c" else OptionRight.PUT


def parse_options(text: str) -> list[tuple[str, OptionDetail, str]]:
    """Return ``(root_symbol, OptionDetail, surface_text)`` for each option found.

    De-duplicates on (root, strike, right) so the same contract written twice in
    a post yields one entry.
    """
    found: dict[tuple[str, float, str], tuple[str, OptionDetail, str]] = {}
    for regex in (_RE_STRIKE_RIGHT, _RE_RIGHT_STRIKE, _RE_WORD):
        for m in regex.finditer(text or ""):
            root = m.group("root").upper()
            right = _right(m.group("right"))
            strike = float(m.group("strike"))
            expiry_raw, dte, leaps = _parse_expiry(m.group("rest"))
            key = (root, strike, right.value)
            if key in found:
                continue
            found[key] = (
                root,
                OptionDetail(
                    right=right,
                    strike=strike,
                    expiry_raw=expiry_raw,
                    dte_relative=dte,
                    is_leaps=leaps,
                ),
                m.group(0),
            )
    return list(found.values())
