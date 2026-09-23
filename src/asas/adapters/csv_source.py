"""CSV directory reader. Files are opened read-only; PIT filtering happens after parsing
(timestamps are only comparable once typed), before any record is used."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from asas.adapters.mapped import MappedSource, Row
from asas.fields import DataContractError
from asas.mapping import EntityMapping, SourceMapping


class CsvReader:
    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)

    def _path(self, source: str) -> Path:
        path = (self._dir / source).resolve()
        if self._dir.resolve() not in path.parents:
            raise DataContractError(f"source {source!r} escapes the data directory")
        if not path.is_file():
            raise DataContractError(f"source file not found: {path}")
        return path

    def columns(self, source: str) -> tuple[str, ...]:
        with open(self._path(source), encoding="utf-8-sig", newline="") as fh:
            return tuple(next(csv.reader(fh), []))

    def rows(self, em: EntityMapping, as_of: datetime) -> Iterator[Row]:
        with open(self._path(em.source), encoding="utf-8-sig", newline="") as fh:
            yield from csv.DictReader(fh)


def csv_source(data_dir: str | Path, mapping: SourceMapping) -> MappedSource:
    return MappedSource(CsvReader(data_dir), mapping)
