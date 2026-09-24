"""SQLite store (Postgres-portable SQL). Every table is append-only, enforced by triggers.

Writers use a short-lived read-write connection under a process lock; everything an agent
tool can reach goes through `reader()`, a `mode=ro` + `query_only` connection (rail 13).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from asas.core.errors import DataContractError, ImmutableRecordError
from asas.core.ids import canonical_json, content_hash
from asas.data.ingest import SourceBundle
from asas.domain.models import (
    Alert,
    AlertAnnex,
    ReviewOutcome,
    RfiEvent,
    TradeEvent,
    TradePerson,
)

SCHEMA_VERSION = 1
M = TypeVar("M", bound=BaseModel)

_TABLES = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trade_events (
  trade_id TEXT NOT NULL, version INTEGER NOT NULL, record_ts TEXT NOT NULL,
  payload TEXT NOT NULL, payload_hash TEXT NOT NULL, PRIMARY KEY (trade_id, version));
CREATE TABLE IF NOT EXISTS trade_persons (
  trade_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_annexes (
  alert_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rfi_events (
  event_key TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,
  version TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL, UNIQUE (kind, key, version));
CREATE INDEX IF NOT EXISTS artifacts_kind_key ON artifacts (kind, key, seq);
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, agent TEXT NOT NULL,
  subject TEXT NOT NULL, manifest TEXT NOT NULL, started_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS agent_runs_key ON agent_runs (idempotency_key);
CREATE TABLE IF NOT EXISTS agent_run_results (
  run_id TEXT PRIMARY KEY, status TEXT NOT NULL, finished_at TEXT NOT NULL,
  result TEXT NOT NULL, result_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_steps (
  run_id TEXT NOT NULL, step INTEGER NOT NULL, policy TEXT NOT NULL, action TEXT NOT NULL,
  observation TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, step));
CREATE TABLE IF NOT EXISTS tool_calls (
  call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step INTEGER NOT NULL, tool TEXT NOT NULL,
  args TEXT NOT NULL, result TEXT NOT NULL, result_hash TEXT NOT NULL, cached INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_cache (
  request_hash TEXT PRIMARY KEY, model_id TEXT NOT NULL, response TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT, name TEXT NOT NULL,
  start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, status TEXT NOT NULL,
  attributes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL);
"""

APPEND_ONLY = (
    "trade_events",
    "trade_persons",
    "alerts",
    "alert_annexes",
    "rfi_events",
    "outcomes",
    "artifacts",
    "agent_runs",
    "agent_run_results",
    "agent_steps",
    "tool_calls",
    "model_cache",
    "spans",
    "audit_log",
)
GENESIS = "0" * 64


def ts_key(value: datetime) -> str:
    """Fixed-width UTC text so lexical order equals time order."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self.writer() as conn:
            conn.executescript(_TABLES)
            for table in APPEND_ONLY:
                for op in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
                    )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version VALUES (?, ?)",
                (SCHEMA_VERSION, ts_key(utcnow())),
            )

    # ------------------------------------------------------------ connections

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._conn
            try:
                yield conn
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "append-only" in str(exc):
                    raise ImmutableRecordError(str(exc)) from exc
                raise
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ source snapshots

    def ingest(self, bundle: SourceBundle) -> dict[str, int]:
        """Idempotent append. Re-sending an identical row is a no-op; a changed row with the
        same key is rejected: sources are bitemporal, corrections arrive as new versions."""
        counts: dict[str, int] = {}
        with self.writer() as conn:
            counts["trade_events"] = self._append(
                conn,
                "trade_events",
                ("trade_id", "version"),
                [((e.trade_id, e.version), e.record_time, e) for e in bundle.trade_events],
            )
            counts["trade_persons"] = self._append(
                conn,
                "trade_persons",
                ("trade_id",),
                [((p.trade_id,), p.record_time, p) for p in bundle.trade_persons],
            )
            counts["alerts"] = self._append(
                conn,
                "alerts",
                ("alert_id",),
                [((a.alert_id,), a.record_time, a) for a in bundle.alerts],
            )
            counts["alert_annexes"] = self._append(
                conn,
                "alert_annexes",
                ("alert_id",),
                [((a.alert_id,), a.record_time, a) for a in bundle.alert_annexes],
            )
            counts["rfi_events"] = self._append(
                conn,
                "rfi_events",
                ("event_key",),
                [
                    ((f"{r.alert_id}|{r.action.value}|{ts_key(r.record_time)}",), r.record_time, r)
                    for r in bundle.rfi_events
                ],
            )
            counts["outcomes"] = self._append(
                conn,
                "outcomes",
                ("outcome_id",),
                [((o.outcome_id,), o.decided_at, o) for o in bundle.outcomes],
            )
        return counts

    @staticmethod
    def _append(
        conn: sqlite3.Connection,
        table: str,
        key_cols: tuple[str, ...],
        rows: list[tuple[tuple[Any, ...], datetime, BaseModel]],
    ) -> int:
        added = 0
        where = " AND ".join(f"{c} = ?" for c in key_cols)
        cols = ", ".join((*key_cols, "record_ts", "payload", "payload_hash"))
        marks = ", ".join("?" for _ in range(len(key_cols) + 3))
        for key, record_time, model in rows:
            payload = model.model_dump_json()
            digest = content_hash(payload)
            existing = conn.execute(
                f"SELECT payload_hash FROM {table} WHERE {where}", key
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise DataContractError(
                        f"{table} {key} changed in place; corrections must be new versions"
                    )
                continue
            conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                (*key, ts_key(record_time), payload, digest),
            )
            added += 1
        return added

    def load_bundle(self, as_of: datetime) -> SourceBundle:
        """Point-in-time read (rail 10): nothing recorded after `as_of` is returned."""
        cut = ts_key(as_of)
        with self.reader() as conn:

            def rows(table: str, model: type[M], strict_before: bool = False) -> tuple[M, ...]:
                op = "<" if strict_before else "<="
                cur = conn.execute(
                    f"SELECT payload FROM {table} WHERE record_ts {op} ? ORDER BY record_ts, rowid",
                    (cut,),
                )
                return tuple(model.model_validate_json(r[0]) for r in cur)

            return SourceBundle(
                trade_events=rows("trade_events", TradeEvent),
                trade_persons=rows("trade_persons", TradePerson),
                alerts=rows("alerts", Alert),
                alert_annexes=rows("alert_annexes", AlertAnnex),
                rfi_events=rows("rfi_events", RfiEvent),
                outcomes=rows("outcomes", ReviewOutcome, strict_before=True),
            )

    def data_watermark(self) -> datetime | None:
        with self.reader() as conn:
            row = conn.execute(
                "SELECT MAX(record_ts) FROM (SELECT record_ts FROM trade_events "
                "UNION ALL SELECT record_ts FROM alerts)"
            ).fetchone()
        if not row or row[0] is None:
            return None
        return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)

    # ------------------------------------------------------------ versioned artifacts

    def put_artifact(self, kind: str, key: str, version: str, obj: Any) -> str:
        payload = obj.model_dump_json() if isinstance(obj, BaseModel) else canonical_json(obj)
        digest = content_hash(payload)
        with self.writer() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM artifacts WHERE kind=? AND key=? AND version=?",
                (kind, key, version),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ImmutableRecordError(f"artifact {kind}/{key}@{version} is immutable")
                return digest
            conn.execute(
                "INSERT INTO artifacts (kind, key, version, created_at, payload, payload_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, key, version, ts_key(utcnow()), payload, digest),
            )
        return digest

    def get_artifact(self, kind: str, key: str, version: str | None = None) -> str | None:
        with self.reader() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT payload FROM artifacts WHERE kind=? AND key=? "
                    "ORDER BY seq DESC LIMIT 1",
                    (kind, key),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload FROM artifacts WHERE kind=? AND key=? AND version=?",
                    (kind, key, version),
                ).fetchone()
        return None if row is None else str(row[0])

    def get_model(
        self, kind: str, key: str, model: type[M], version: str | None = None
    ) -> M | None:
        payload = self.get_artifact(kind, key, version)
        return None if payload is None else model.model_validate_json(payload)

    def list_artifacts(self, kind: str) -> list[tuple[str, str, str]]:
        """Latest version of every key of a kind: (key, version, payload)."""
        with self.reader() as conn:
            cur = conn.execute(
                "SELECT a.key, a.version, a.payload FROM artifacts a JOIN ("
                "  SELECT key, MAX(seq) AS seq FROM artifacts WHERE kind=? GROUP BY key"
                ") m ON a.seq = m.seq ORDER BY a.key",
                (kind,),
            )
            return [(str(r[0]), str(r[1]), str(r[2])) for r in cur]

    def artifact_history(self, kind: str, key: str) -> list[tuple[str, str, str]]:
        with self.reader() as conn:
            cur = conn.execute(
                "SELECT version, created_at, payload FROM artifacts WHERE kind=? AND key=? "
                "ORDER BY seq",
                (kind, key),
            )
            return [(str(r[0]), str(r[1]), str(r[2])) for r in cur]

    # ------------------------------------------------------------ audit chain

    def audit(self, actor: str, action: str, subject: str, detail: dict[str, Any]) -> str:
        with self.writer() as conn:
            row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            at = ts_key(utcnow())
            body = canonical_json(
                {
                    "at": at,
                    "actor": actor,
                    "action": action,
                    "subject": subject,
                    "detail": detail,
                    "prev": prev,
                }
            )
            digest = content_hash(body)
            conn.execute(
                "INSERT INTO audit_log (at, actor, action, subject, detail, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at, actor, action, subject, canonical_json(detail), prev, digest),
            )
        return digest

    def verify_audit_chain(self) -> tuple[bool, int]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT at, actor, action, subject, detail, prev_hash, hash FROM audit_log "
                "ORDER BY seq"
            ).fetchall()
        prev = GENESIS
        for at, actor, action, subject, detail, prev_hash, digest in rows:
            body = canonical_json(
                {
                    "at": at,
                    "actor": actor,
                    "action": action,
                    "subject": subject,
                    "detail": json.loads(detail),
                    "prev": prev,
                }
            )
            if prev_hash != prev or content_hash(body) != digest:
                return False, len(rows)
            prev = digest
        return True, len(rows)

    def audit_entries(self, limit: int) -> list[dict[str, Any]]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT seq, at, actor, action, subject, detail, hash FROM audit_log "
                "ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "seq": r[0],
                "at": r[1],
                "actor": r[2],
                "action": r[3],
                "subject": r[4],
                "detail": json.loads(r[5]),
                "hash": r[6],
            }
            for r in rows
        ]

    # ------------------------------------------------------------ generic append helpers

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.writer() as conn:
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def insert_ignore(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.writer() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())
            )

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise PermissionError("query() is SELECT-only")
        with self.reader() as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]
