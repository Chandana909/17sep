"""Versioned source -> contract column mapping (rail 16).

This is the ONLY place real SCP/CAL column names live. Integrating a new data drop means
editing a mapping TOML file (see docs/INTEGRATION.md), never Python decision code.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from asas.fields import (
    ENTITY_FIELDS,
    REQUIRED_FIELDS,
    TIMESTAMP_FIELDS,
    DataContractError,
)

ENTITIES = ("alerts", "trade_events", "rfi_events", "past_cases")
NUMERIC_FIELDS = frozenset({"PRICE", "QUANTITY"})
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_HOOK = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")

RowHook = Callable[[dict[str, object]], dict[str, object]]


def _offset(text: str | None) -> timezone | None:
    if text is None:
        return None
    match = _OFFSET.match(text)
    if not match:
        raise DataContractError(f"naive_timestamp_offset must look like +00:00, got {text!r}")
    sign = -1 if match.group(1) == "-" else 1
    return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))


def _load_hook(spec: str | None) -> RowHook | None:
    if spec is None:
        return None
    if not _HOOK.match(spec):
        raise DataContractError(f"transform must be 'module:function', got {spec!r}")
    module, func = spec.split(":")
    hook = getattr(importlib.import_module(module), func)
    if not callable(hook):
        raise DataContractError(f"transform {spec!r} is not callable")
    return hook  # type: ignore[no-any-return]


@dataclass(frozen=True)
class EntityMapping:
    entity: str
    source: str  # CSV file name or SQL table/view name
    columns: Mapping[str, str]  # source column -> contract field
    ignore: frozenset[str] = frozenset()  # present in source, deliberately unused
    derived: frozenset[str] = frozenset()  # produced by `transform`, not physically present
    values: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    tz: timezone | None = None  # applied to naive timestamps only
    timestamp_format: str | None = None
    transform: RowHook | None = None

    @property
    def mapped_fields(self) -> frozenset[str]:
        return frozenset(self.columns.values())

    @property
    def missing_required(self) -> frozenset[str]:
        return REQUIRED_FIELDS[self.entity] - self.mapped_fields

    def source_column_for(self, contract_field: str) -> str | None:
        return next((src for src, f in self.columns.items() if f == contract_field), None)

    @property
    def physical_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c not in self.derived)

    def to_contract(self, raw: Mapping[str, object]) -> dict[str, object]:
        row = dict(raw)
        if self.transform is not None:
            row = self.transform(row)
        out: dict[str, object] = {}
        for src, contract_field in self.columns.items():
            if src in row:
                out[contract_field] = self._value(contract_field, row[src])
        return out

    def _value(self, contract_field: str, value: object) -> object:
        vmap = self.values.get(contract_field)
        if vmap is not None and value is not None and str(value).strip() in vmap:
            value = vmap[str(value).strip()]
        if contract_field in TIMESTAMP_FIELDS:
            return self._timestamp(contract_field, value)
        if contract_field in NUMERIC_FIELDS:
            return value
        if isinstance(value, int | Decimal) and not isinstance(value, bool):
            return str(value)
        return value

    def _timestamp(self, contract_field: str, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                if self.timestamp_format:
                    value = datetime.strptime(text, self.timestamp_format)
                else:
                    value = datetime.fromisoformat(text)
            except ValueError as exc:
                raise DataContractError(
                    f"{contract_field}: unparseable timestamp {text!r}"
                ) from exc
        if isinstance(value, datetime) and value.tzinfo is None and self.tz is not None:
            value = value.replace(tzinfo=self.tz)
        return value


@dataclass(frozen=True)
class SqlOptions:
    paramstyle: str = "qmark"  # qmark | named | format | pyformat | numeric
    as_of_type: str = "datetime"  # datetime | naive_utc | iso
    quote: str = '"'


@dataclass(frozen=True)
class SourceMapping:
    version: str
    entities: Mapping[str, EntityMapping]
    file_ignore: Mapping[str, frozenset[str]] = field(default_factory=dict)
    sql: SqlOptions = field(default_factory=SqlOptions)

    def declared_columns(self, source: str) -> frozenset[str]:
        declared: set[str] = set(self.file_ignore.get(source, frozenset()))
        for em in self.entities.values():
            if em.source == source:
                declared |= set(em.physical_columns) | em.ignore
        return frozenset(declared)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({em.source for em in self.entities.values()}))


def _str_list(value: Any, where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DataContractError(f"{where} must be a list of strings")
    return frozenset(value)


def _entity(name: str, spec: Mapping[str, Any]) -> EntityMapping:
    if not isinstance(spec.get("source"), str) or not spec["source"]:
        raise DataContractError(f"{name}.source is required")
    columns = spec.get("columns", {})
    if not isinstance(columns, dict) or not all(isinstance(v, str) for v in columns.values()):
        raise DataContractError(f"{name}.columns must map source column -> contract field")
    unknown = sorted(set(columns.values()) - ENTITY_FIELDS[name])
    if unknown:
        raise DataContractError(f"{name}.columns targets fields not in the contract: {unknown}")
    targets = list(columns.values())
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        raise DataContractError(f"{name}.columns maps several columns to {dupes}")
    values = spec.get("values", {})
    for vfield, vmap in values.items():
        if vfield not in ENTITY_FIELDS[name] or not isinstance(vmap, dict):
            raise DataContractError(f"{name}.values.{vfield} is invalid")
    return EntityMapping(
        entity=name,
        source=spec["source"],
        columns=dict(columns),
        ignore=_str_list(spec.get("ignore"), f"{name}.ignore"),
        derived=_str_list(spec.get("derived"), f"{name}.derived"),
        values={k: {str(a): str(b) for a, b in v.items()} for k, v in values.items()},
        tz=_offset(spec.get("naive_timestamp_offset")),
        timestamp_format=spec.get("timestamp_format"),
        transform=_load_hook(spec.get("transform")),
    )


def mapping_from_dict(data: Mapping[str, Any]) -> SourceMapping:
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise DataContractError("mapping.version is required")
    unknown = sorted(set(data) - {"version", "files", "sql", *ENTITIES})
    if unknown:
        raise DataContractError(f"unknown mapping sections: {unknown}")
    files = {
        src: _str_list(spec.get("ignore"), f"files.{src}.ignore")
        for src, spec in data.get("files", {}).items()
    }
    sql = SqlOptions(**data.get("sql", {}))
    if sql.paramstyle not in {"qmark", "named", "format", "pyformat", "numeric"}:
        raise DataContractError(f"sql.paramstyle {sql.paramstyle!r} unsupported")
    if sql.as_of_type not in {"datetime", "naive_utc", "iso"}:
        raise DataContractError(f"sql.as_of_type {sql.as_of_type!r} unsupported")
    entities = {name: _entity(name, data[name]) for name in ENTITIES if name in data}
    return SourceMapping(version, entities, files, sql)


def load_mapping(path: str | Path) -> SourceMapping:
    with open(path, "rb") as fh:
        return mapping_from_dict(tomllib.load(fh))
