"""Dependency-free text helpers (shared by the DB layer and the deduper)."""

from __future__ import annotations

import hashlib
import re

_URL_RE = re.compile(r"https?://\S+")
_TAG_RE = re.compile(r"[@#]\w+")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, drop URLs / @mentions / #hashtags / punctuation, collapse
    whitespace — so near-duplicate posts (incl. cross-provider) hash equal."""
    t = _URL_RE.sub("", text or "").lower()
    t = _TAG_RE.sub("", t)
    t = re.sub(r"[^a-z0-9$ ]", "", t)
    return _WS_RE.sub(" ", t).strip()


def content_sha(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
