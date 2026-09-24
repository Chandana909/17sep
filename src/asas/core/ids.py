"""Deterministic identifiers, hashing and canonical JSON."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

_SEP = "\x1f"
_ID_HEX = 16


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump(mode="python"))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        return sorted(to_jsonable(v) for v in obj)
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(obj: Any) -> str:
    text = obj if isinstance(obj, str) else canonical_json(obj)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:_ID_HEX]}"


def fraction_from_hash(*parts: str) -> Decimal:
    """Deterministic value in [0, 1) used for reproducible sampling."""
    digest = hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
    return Decimal(int(digest[:_ID_HEX], 16)) / Decimal(16**_ID_HEX)
