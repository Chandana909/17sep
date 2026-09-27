"""Storage dialects: SQLite (single node, zero setup) and PostgreSQL (production).

The Store speaks one small SQL subset through a `Conn` wrapper that translates `?`
placeholders. Everything dialect-specific stays here:
- how a writer and a reader connect (readers are read-only at the database level)
- how "insert unless present" is spelled
- how the audit chain is serialised across processes (a Postgres advisory lock)
- which driver errors mean "append-only table"
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from asas.core.errors import DuplicateKeyError, ImmutableRecordError

APPEND_ONLY_MESSAGE = "append-only table"
AUDIT_LOCK_KEY = 7_417_213  # arbitrary, stable advisory-lock id for the audit chain


class Conn:
    """A DB-API connection that accepts `?` placeholders on every dialect."""

    def __init__(self, raw: Any, paramstyle: str) -> None:
        self.raw = raw
        self._format = paramstyle == "format"

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self._format:
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self.raw.execute(sql, tuple(params))


class Dialect(Protocol):
    name: str
    url: str

    def writer(self) -> Iterator[Conn]: ...
    def reader(self) -> Iterator[Conn]: ...
    def close(self) -> None: ...
    def insert_ignore(self, table: str, cols: Sequence[str]) -> str: ...
    def lock_audit_chain(self, conn: Conn) -> None: ...


def _translate(exc: Exception) -> Exception:
    text = str(exc)
    if APPEND_ONLY_MESSAGE in text:
        return ImmutableRecordError(text)
    if "UNIQUE constraint failed" in text or "duplicate key value" in text:
        return DuplicateKeyError(text)
    return exc


class SqliteDialect:
    name = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.url = f"sqlite:///{self.path}"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def writer(self) -> Iterator[Conn]:
        """Serialised in-process by a lock and across processes by BEGIN IMMEDIATE, so a
        read-then-write (the audit chain head, idempotent inserts) is atomic even when
        a CLI job and the API server share the file."""
        with self._lock:
            try:
                if not self._conn.in_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield Conn(self._conn, "qmark")
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise _translate(exc) from exc
            except Exception:
                self._conn.rollback()
                raise

    @contextmanager
    def reader(self) -> Iterator[Conn]:
        conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield Conn(conn, "qmark")
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_ignore(self, table: str, cols: Sequence[str]) -> str:
        return (
            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})"
        )

    def lock_audit_chain(self, conn: Conn) -> None:
        """The process-wide writer lock already serialises SQLite writers."""


class PostgresDialect:
    """psycopg 3. One writer connection under a process lock (serialised writes, like the
    SQLite writer) plus a small pool of read-only reader connections. A separate
    `reader_url` lets readers use a database role that only has SELECT."""

    name = "postgres"

    def __init__(self, url: str, reader_url: str | None = None, pool_size: int = 8) -> None:
        import psycopg

        self._psycopg = psycopg
        self.url = url
        self._reader_url = reader_url or url
        self._lock = threading.RLock()
        self._writer = psycopg.connect(url)
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=pool_size)
        self._closed = False

    @contextmanager
    def writer(self) -> Iterator[Conn]:
        psycopg = self._psycopg
        with self._lock:
            conn = self._writer
            try:
                yield Conn(conn, "format")
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                raise _translate(exc) from exc
            except Exception:
                conn.rollback()
                raise

    def _new_reader(self) -> Any:
        conn = self._psycopg.connect(self._reader_url, autocommit=True)
        conn.execute("SET default_transaction_read_only = on")
        return conn

    @contextmanager
    def reader(self) -> Iterator[Conn]:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._new_reader()
        healthy = True
        try:
            yield Conn(conn, "format")
        except self._psycopg.OperationalError:
            healthy = False
            raise
        finally:
            if healthy and not conn.closed and not self._closed:
                try:
                    self._pool.put_nowait(conn)
                except queue.Full:
                    conn.close()
            else:
                conn.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._writer.close()
            while not self._pool.empty():
                self._pool.get_nowait().close()

    def insert_ignore(self, table: str, cols: Sequence[str]) -> str:
        return (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT DO NOTHING"
        )

    def lock_audit_chain(self, conn: Conn) -> None:
        """Serialise chain appends across processes and replicas for this transaction."""
        conn.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LOCK_KEY,))


def open_dialect(
    target: str | Path, reader_url: str | None = None
) -> SqliteDialect | PostgresDialect:
    text = str(target)
    if text.startswith(("postgresql://", "postgres://")):
        return PostgresDialect(text, reader_url)
    if text.startswith("sqlite:///"):
        return SqliteDialect(text.removeprefix("sqlite:///"))
    return SqliteDialect(text)
