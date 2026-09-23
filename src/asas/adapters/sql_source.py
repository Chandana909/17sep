"""DB-API reader. Issues only generated, parameterised SELECT statements with the
point-in-time predicate pushed down (rails 1, 10). Connect with a SELECT-only role."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asas.adapters.mapped import MappedSource, Row
from asas.fields import DataContractError
from asas.mapping import EntityMapping, SourceMapping, SqlOptions

_IDENT_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
_PLACEHOLDER = {
    "qmark": "?",
    "format": "%s",
    "pyformat": "%(as_of)s",
    "named": ":as_of",
    "numeric": ":1",
}

Connect = Callable[[], Any]


class SqlReader:
    def __init__(self, connect: Connect, options: SqlOptions) -> None:
        self._connect = connect
        self._options = options
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def quote(self, identifier: str) -> str:
        parts = identifier.split(".")
        if not all(_IDENT_PART.match(p) for p in parts):
            raise DataContractError(f"unsafe SQL identifier {identifier!r}")
        q = self._options.quote
        return ".".join(f"{q}{p}{q}" for p in parts)

    def _param(self, as_of: datetime) -> Any:
        value: Any = as_of
        if self._options.as_of_type == "naive_utc":
            value = as_of.astimezone(UTC).replace(tzinfo=None)
        elif self._options.as_of_type == "iso":
            value = as_of.astimezone(UTC).isoformat()
        style = self._options.paramstyle
        return {"as_of": value} if style in {"named", "pyformat"} else (value,)

    def build_query(self, em: EntityMapping, as_of: datetime) -> tuple[str, Any]:
        time_field, op = ("DECIDED_AT", "<") if em.entity == "past_cases" else ("RECORD_TIME", "<=")
        time_col = em.source_column_for(time_field)
        if time_col is None or time_col in em.derived:
            raise DataContractError(f"{em.entity}: {time_field} must map to a physical column")
        cols = ", ".join(self.quote(c) for c in em.physical_columns)
        sql = (
            f"SELECT {cols} FROM {self.quote(em.source)} "
            f"WHERE {self.quote(time_col)} {op} {_PLACEHOLDER[self._options.paramstyle]}"
        )
        return sql, self._param(as_of)

    def _select(self, sql: str, params: Any) -> tuple[list[str], list[Any]]:
        cur = self._connection().cursor()
        try:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return names, cur.fetchall()
        finally:
            cur.close()

    def columns(self, source: str) -> tuple[str, ...]:
        cur = self._connection().cursor()
        try:
            cur.execute(f"SELECT * FROM {self.quote(source)} WHERE 1 = 0")
            return tuple(d[0] for d in cur.description)
        finally:
            cur.close()

    def rows(self, em: EntityMapping, as_of: datetime) -> Iterator[Row]:
        names, rows = self._select(*self.build_query(em, as_of))
        for row in rows:
            yield dict(zip(names, row, strict=True))


def sqlite_readonly(path: str | Path) -> Connect:
    uri = Path(path).resolve().as_uri() + "?mode=ro"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    return connect


def sql_source(connect: Connect, mapping: SourceMapping) -> MappedSource:
    return MappedSource(SqlReader(connect, mapping.sql), mapping)
