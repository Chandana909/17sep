"""Versioned schema migrations for every storage dialect.

The schema is declared once (`TABLES`) and rendered per dialect. Each applied migration is
recorded in `schema_migrations` with a checksum of the exact statements; if a released
migration is edited later, start-up fails instead of silently diverging. To change the
schema, append a new `Migration` and never edit an old one.

Every table is append-only, enforced by the database itself:
- SQLite: BEFORE UPDATE/DELETE triggers
- Postgres: row triggers plus a TRUNCATE trigger (and revoke UPDATE/DELETE/TRUNCATE from the
  application role, see deploy/postgres/roles.sql)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from asas.core.errors import AsasError
from asas.core.ids import content_hash
from asas.store.dialect import APPEND_ONLY_MESSAGE, Conn

SEQ = "{SEQ}"  # auto-incrementing primary key, rendered per dialect

TABLES: tuple[tuple[str, str], ...] = (
    (
        "trade_events",
        "trade_id TEXT NOT NULL, version INTEGER NOT NULL, record_ts TEXT NOT NULL, "
        "payload TEXT NOT NULL, payload_hash TEXT NOT NULL, PRIMARY KEY (trade_id, version)",
    ),
    (
        "trade_persons",
        "trade_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL",
    ),
    (
        "alerts",
        "alert_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL",
    ),
    (
        "alert_annexes",
        "alert_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL",
    ),
    (
        "rfi_events",
        "event_key TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL",
    ),
    (
        "outcomes",
        "outcome_id TEXT PRIMARY KEY, record_ts TEXT NOT NULL, payload TEXT NOT NULL, "
        "payload_hash TEXT NOT NULL",
    ),
    (
        "artifacts",
        f"{SEQ}, kind TEXT NOT NULL, key TEXT NOT NULL, version TEXT NOT NULL, "
        "created_at TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, "
        "UNIQUE (kind, key, version)",
    ),
    (
        "agent_runs",
        "run_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, agent TEXT NOT NULL, "
        "subject TEXT NOT NULL, manifest TEXT NOT NULL, started_at TEXT NOT NULL",
    ),
    (
        "agent_run_results",
        "run_id TEXT PRIMARY KEY, status TEXT NOT NULL, finished_at TEXT NOT NULL, "
        "result TEXT NOT NULL, result_hash TEXT NOT NULL",
    ),
    (
        "agent_steps",
        "run_id TEXT NOT NULL, step INTEGER NOT NULL, policy TEXT NOT NULL, "
        "action TEXT NOT NULL, observation TEXT NOT NULL, state TEXT NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY (run_id, step)",
    ),
    (
        "tool_calls",
        "call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step INTEGER NOT NULL, "
        "tool TEXT NOT NULL, args TEXT NOT NULL, result TEXT NOT NULL, "
        "result_hash TEXT NOT NULL, cached INTEGER NOT NULL, duration_ms INTEGER NOT NULL, "
        "error TEXT NOT NULL, created_at TEXT NOT NULL",
    ),
    (
        "model_cache",
        "request_hash TEXT PRIMARY KEY, model_id TEXT NOT NULL, response TEXT NOT NULL, "
        "created_at TEXT NOT NULL",
    ),
    (
        "spans",
        "span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT, "
        "name TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, "
        "status TEXT NOT NULL, attributes TEXT NOT NULL",
    ),
    (
        "audit_log",
        f"{SEQ}, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, "
        "subject TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL, "
        "hash TEXT NOT NULL",
    ),
)
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("artifacts_kind_key", "artifacts", "kind, key, seq"),
    ("agent_runs_key", "agent_runs", "idempotency_key"),
    ("agent_runs_subject", "agent_runs", "subject"),
    ("trade_events_record", "trade_events", "record_ts"),
    ("alerts_record", "alerts", "record_ts"),
    ("tool_calls_run", "tool_calls", "run_id, step"),
)
APPEND_ONLY = tuple(name for name, _ in TABLES)


def _columns(dialect: str, spec: str) -> str:
    if dialect == "postgres":
        spec = spec.replace("INTEGER", "BIGINT")
        return spec.replace(SEQ, "seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    return spec.replace(SEQ, "seq INTEGER PRIMARY KEY AUTOINCREMENT")


def _initial(dialect: str) -> tuple[str, ...]:
    out = [
        f"CREATE TABLE IF NOT EXISTS {name} ({_columns(dialect, spec)})" for name, spec in TABLES
    ]
    out += [f"CREATE INDEX IF NOT EXISTS {n} ON {t} ({c})" for n, t, c in INDEXES]
    if dialect == "postgres":
        out.append(
            "CREATE OR REPLACE FUNCTION asas_append_only() RETURNS trigger LANGUAGE plpgsql AS "
            f"$$ BEGIN RAISE EXCEPTION '{APPEND_ONLY_MESSAGE}: %', TG_TABLE_NAME; END $$"
        )
        for table in APPEND_ONLY:
            out.append(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            out.append(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION asas_append_only()"
            )
            out.append(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
            out.append(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION asas_append_only()"
            )
    else:
        for table in APPEND_ONLY:
            for op in ("UPDATE", "DELETE"):
                out.append(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MESSAGE}'); END"
                )
    return tuple(out)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str

    def statements(self, dialect: str) -> tuple[str, ...]:
        if self.version == 1:
            return _initial(dialect)
        raise AsasError(f"migration {self.version} has no statements")  # pragma: no cover

    def checksum(self, dialect: str) -> str:
        return content_hash(list(self.statements(dialect)))


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial append-only schema"),)


class MigrationError(AsasError):
    """The database schema does not match the released migrations."""


def _has_ledger(conn: Conn, dialect: str) -> bool:
    if dialect == "postgres":
        row = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
        return row is not None and row[0] is not None
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    return row is not None


def _applied(conn: Conn, dialect: str) -> dict[int, str]:
    if not _has_ledger(conn, dialect):
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(v): str(c) for v, c in rows}


def pending(
    conn: Conn, dialect: str, migrations: Sequence[Migration] = MIGRATIONS
) -> list[Migration]:
    """Migrations not yet applied; raises if an applied one was edited since."""
    done = _applied(conn, dialect)
    out = []
    for m in sorted(migrations, key=lambda m: m.version):
        if m.version not in done:
            out.append(m)
        elif done[m.version] != m.checksum(dialect):
            raise MigrationError(
                f"migration {m.version} ({m.name}) changed after it was applied; "
                "add a new migration instead of editing a released one"
            )
    return out


def migrate(
    conn: Conn, dialect: str, now: str, migrations: Sequence[Migration] = MIGRATIONS
) -> list[int]:
    """Apply pending migrations inside the caller's transaction; returns applied versions.
    Issues no DDL at all when the schema is current (the application role needs none)."""
    todo = pending(conn, dialect, migrations)
    if todo and not _has_ledger(conn, dialect):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
    applied: list[int] = []
    for m in todo:
        checksum = m.checksum(dialect)
        for statement in m.statements(dialect):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (m.version, m.name, checksum, now),
        )
        applied.append(m.version)
    return applied
