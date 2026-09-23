"""Versioned config access. Decision code must use `require`; a missing key raises
`ConfigMissing` and the caller fails safe (individual review). Never invent a default."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class ConfigMissing(KeyError):
    """A decision-relevant config key is absent or malformed."""


@dataclass(frozen=True, eq=False)
class Config:
    data: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.require("version"))

    def require(self, *path: str) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise ConfigMissing(".".join(path))
            node = node[key]
        return node

    def decimal(self, *path: str) -> Decimal:
        value = self.require(*path)
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise ConfigMissing(".".join(path))
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ConfigMissing(".".join(path)) from exc
        if not result.is_finite():
            raise ConfigMissing(".".join(path))
        return result

    def integer(self, *path: str) -> int:
        value = self.require(*path)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigMissing(".".join(path))
        return value

    def strings(self, *path: str) -> tuple[str, ...]:
        value = self.require(*path)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigMissing(".".join(path))
        return tuple(value)

    def without(self, *path: str) -> Config:
        """Copy with one key removed. Used by fail-safe tests."""
        data = copy.deepcopy(self.data)
        node = data
        for key in path[:-1]:
            node = node[key]
        node.pop(path[-1], None)
        return Config(data)

    def with_value(self, value: Any, *path: str) -> Config:
        data = copy.deepcopy(self.data)
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
        return Config(data)


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        cfg = Config(tomllib.load(fh))
    cfg.version  # noqa: B018 - fail loudly if unversioned
    return cfg
