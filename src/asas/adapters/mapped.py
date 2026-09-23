"""A ReadOnlySource built from any RowReader plus a versioned SourceMapping."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol

from asas.fields import UnknownFieldError
from asas.ingest import parse_alerts, parse_past_cases, parse_rfi_events, parse_trade_events
from asas.mapping import ENTITIES, EntityMapping, SourceMapping
from asas.models import Alert, AlertAnnex, PastCase, RfiEvent, TradeEvent
from asas.sources import decided_before, visible

Row = Mapping[str, object]


class RowReader(Protocol):
    """Reads raw rows. Implementations must be read-only."""

    def columns(self, source: str) -> tuple[str, ...]: ...
    def rows(self, em: EntityMapping, as_of: datetime) -> Iterable[Row]: ...


class MappedSource:
    def __init__(self, reader: RowReader, mapping: SourceMapping) -> None:
        self._reader = reader
        self._mapping = mapping
        self._checked = False
        self._cache: dict[tuple[str, datetime], list[dict[str, object]]] = {}

    @property
    def mapping_version(self) -> str:
        return self._mapping.version

    @property
    def unavailable(self) -> frozenset[str]:
        return frozenset(e for e in ENTITIES if e not in self._mapping.entities)

    def schema_problems(self) -> list[str]:
        """Source columns not declared anywhere in the mapping (schema drift)."""
        problems: list[str] = []
        for source in self._mapping.sources:
            header = set(self._reader.columns(source))
            undeclared = sorted(header - self._mapping.declared_columns(source))
            if undeclared:
                problems.append(f"{source}: undeclared columns {undeclared}")
            for em in self._mapping.entities.values():
                if em.source == source:
                    absent = sorted(set(em.physical_columns) - header)
                    if absent:
                        problems.append(f"{source}: mapped columns missing {absent}")
        return problems

    def _check_schema(self) -> None:
        if self._checked:
            return
        for source in self._mapping.sources:
            header = set(self._reader.columns(source))
            undeclared = sorted(header - self._mapping.declared_columns(source))
            if undeclared:
                raise UnknownFieldError(undeclared)
        self._checked = True

    def _rows(self, entity: str, as_of: datetime) -> list[dict[str, object]]:
        em = self._mapping.entities.get(entity)
        if em is None:
            return []
        self._check_schema()
        key = (entity, as_of)
        if key not in self._cache:
            self._cache[key] = [em.to_contract(r) for r in self._reader.rows(em, as_of)]
        return self._cache[key]

    def alerts(self, as_of: datetime) -> tuple[Alert, ...]:
        return visible(parse_alerts(self._rows("alerts", as_of))[0], as_of)

    def annexes(self, as_of: datetime) -> tuple[AlertAnnex, ...]:
        return visible(parse_alerts(self._rows("alerts", as_of))[1], as_of)

    def trade_events(self, as_of: datetime) -> tuple[TradeEvent, ...]:
        return visible(parse_trade_events(self._rows("trade_events", as_of)), as_of)

    def rfi_events(self, as_of: datetime) -> tuple[RfiEvent, ...]:
        return visible(parse_rfi_events(self._rows("rfi_events", as_of)), as_of)

    def past_cases(self, as_of: datetime) -> tuple[PastCase, ...]:
        return decided_before(parse_past_cases(self._rows("past_cases", as_of)), as_of)
