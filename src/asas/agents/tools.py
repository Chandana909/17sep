"""Closed, read-only tool registry (rail 13). No generic SQL/HTTP/file tools exist."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType


class ToolNotAllowed(PermissionError):
    pass


class ToolRegistry:
    def __init__(self, tools: Mapping[str, Callable[[str], str]]) -> None:
        self._tools = MappingProxyType(dict(tools))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def call(self, name: str, arg: str) -> str:
        if name not in self._tools:
            raise ToolNotAllowed(name)
        return self._tools[name](arg)
