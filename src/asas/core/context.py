"""Business context catalogue (config/business_context.toml): plain-language meaning, risk
themes and reviewer checks for every hypothesis, signal, rule, reason, category and finding.

Presentation only; decisions never read it. `missing()` lists what a change forgot to
describe, and the test suite fails on any gap, so new logic always ships with its meaning.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from asas.core.errors import ConfigMissing

KINDS = (
    "hypotheses",
    "signals",
    "rules",
    "reasons",
    "categories",
    "corroboration",
    "recommendations",
    "findings",
)


class BusinessContext:
    def __init__(self, data: Mapping[str, Any]) -> None:
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise ConfigMissing("business_context.version")
        unknown = sorted(set(data) - {"version", "owner", *KINDS})
        if unknown:
            raise ConfigMissing(f"business_context: unknown sections {unknown}")
        self.version = version
        self.owner = str(data.get("owner", ""))
        self._data = {k: dict(data.get(k, {})) for k in KINDS}

    def entry(self, kind: str, key: str) -> dict[str, Any] | None:
        """Exact key, else the prefix before ':' (reason and corroboration codes carry detail)."""
        table = self._data.get(kind, {})
        found = table.get(key)
        if found is None and ":" in key:
            found = table.get(key.split(":", 1)[0])
        return dict(found) if isinstance(found, dict) else None

    def meaning(self, kind: str, key: str) -> str:
        found = self.entry(kind, key)
        return str(found.get("meaning", "")) if found else ""

    def missing(self, required: Mapping[str, Iterable[str]]) -> list[str]:
        return sorted(
            f"{kind}.{key}"
            for kind, keys in required.items()
            for key in keys
            if self.entry(kind, key) is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "owner": self.owner, **self._data}


def default_context_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "business_context.toml"


def load_context(path: str | Path | None = None) -> BusinessContext:
    with open(path or default_context_path(), "rb") as fh:
        return BusinessContext(tomllib.load(fh))
