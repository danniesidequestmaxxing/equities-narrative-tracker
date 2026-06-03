"""Telegram MarkdownV2 escaping.

An unescaped reserved char makes Telegram reject the whole message (a dropped
alert). Every dynamic value flows through one of these helpers; ``str()``
coercion means a ``None``/``Decimal``/``BRK.B`` never crashes formatting. See
docs/design/06-telegram-ux.md.
"""

from __future__ import annotations

# The 18 MarkdownV2 reserved characters (text position).
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_MDV2_TRANS = str.maketrans({c: "\\" + c for c in _MDV2_SPECIAL})


def md(text: object) -> str:
    """Escape arbitrary text for a MarkdownV2 *text* position."""
    return str(text).translate(_MDV2_TRANS)


def md_code(text: object) -> str:
    """Escape for inside a ``code`` span: only backtick and backslash."""
    return str(text).replace("\\", "\\\\").replace("`", "\\`")


def md_url(url: object) -> str:
    """Escape a URL used as a link destination: only ``)`` and backslash."""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")
