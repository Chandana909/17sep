"""Deterministic identifiers derived from content."""

from __future__ import annotations

import hashlib

_SEP = "\x1f"
_HEX_LEN = 16


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:_HEX_LEN]}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
