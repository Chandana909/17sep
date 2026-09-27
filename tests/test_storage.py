"""Storage contract on every backend: SQLite always, PostgreSQL when available (a local
embedded server via `pgserver`, or `ASAS_TEST_DATABASE_URL`, e.g. the CI service)."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from asas.core.config import Config
from asas.core.errors import DataContractError, ImmutableRecordError
from asas.data.ingest import SourceBundle
from asas.data.synthetic import SyntheticDataset
from asas.services.demo import DemoResult, run_demo
from asas.store import migrations
from asas.store.db import Store
from asas.store.migrations import MigrationError


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str | None]:
    url = os.environ.get("ASAS_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        yield None
        return
    import tempfile

    server = pgserver.get_server(tempfile.mkdtemp(prefix="asas-pg-"), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


def _fresh_database(base: str) -> str:
    import psycopg

    name = f"asas_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    return base.rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture(params=["sqlite", "postgres"])
def target(request: pytest.FixtureRequest, tmp_path: Path, pg_server: str | None) -> str:
    if request.param == "sqlite":
        return str(tmp_path / "asas.db")
    if pg_server is None:
        pytest.skip("no PostgreSQL available (install pgserver or set ASAS_TEST_DATABASE_URL)")
    return _fresh_database(pg_server)


def test_schema_comes_from_recorded_migrations(target: str) -> None:
    store = Store(target)
    assert store.applied_migrations == [1]
    rows = store.query("SELECT version, checksum FROM schema_migrations")
    assert rows and rows[0][0] == 1 and len(rows[0][1]) == 64
    store.close()
    again = Store(target)
    assert again.applied_migrations == []  # idempotent start-up
    again.close()


def test_an_edited_released_migration_refuses_to_start(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    Store(target).close()
    original = migrations.Migration.statements
    monkeypatch.setattr(
        migrations.Migration,
        "statements",
        lambda self, d: (*original(self, d), "CREATE INDEX IF NOT EXISTS x ON spans (name)"),
    )
    with pytest.raises(MigrationError, match="changed after it was applied"):
        Store(target)


def test_every_table_is_append_only_in_the_database(target: str) -> None:
    store = Store(target)
    store.audit("alice", "DO", "x", {"n": 1})
    store.put_artifact("thing", "k", "1", {"a": 1})
    for sql in (
        "UPDATE audit_log SET actor = 'mallory'",
        "DELETE FROM audit_log",
        "UPDATE artifacts SET payload = '{}'",
        "DELETE FROM artifacts",
    ):
        with pytest.raises(ImmutableRecordError), store.writer() as conn:
            conn.execute(sql)
    if store.dialect.name == "postgres":
        with pytest.raises(ImmutableRecordError), store.writer() as conn:
            conn.execute("TRUNCATE audit_log")
    assert store.verify_audit_chain() == (True, 1)
    store.close()


def test_readers_cannot_write(target: str) -> None:
    store = Store(target)
    with (
        pytest.raises(Exception, match="(?i)read.?only|readonly|attempt to write"),
        store.reader() as conn,
    ):
        conn.execute("INSERT INTO spans VALUES ('s','t',NULL,'n',0,0,'OK','{}')")
    with pytest.raises(PermissionError):
        store.query("DELETE FROM spans")
    store.close()


def test_ingest_is_idempotent_point_in_time_and_immutable(
    target: str, dataset: SyntheticDataset
) -> None:
    store = Store(target)
    head = SourceBundle(
        trade_events=dataset.bundle.trade_events[:300],
        alerts=dataset.bundle.alerts[:40],
        outcomes=dataset.bundle.outcomes[:20],
    )
    first = store.ingest(head)
    assert first["trade_events"] == 300 and store.ingest(head)["trade_events"] == 0
    cut = head.trade_events[150].record_time
    snap = store.load_bundle(cut)
    assert snap.trade_events and all(e.record_time <= cut for e in snap.trade_events)
    assert all(o.decided_at < cut for o in snap.outcomes)
    changed = head.trade_events[0].model_copy(update={"book": "ELSEWHERE"})
    with pytest.raises(DataContractError, match="changed in place"):
        store.ingest(SourceBundle(trade_events=(changed,)))
    assert store.data_watermark() is not None
    store.close()


def test_audit_chain_stays_valid_under_concurrent_writers(target: str) -> None:
    stores = [Store(target), Store(target)]  # two processes / replicas on one database

    def write(store: Store, who: str) -> None:
        for i in range(25):
            store.audit(who, "DO", str(i), {"i": i})

    threads = [threading.Thread(target=write, args=(stores[i % 2], f"w{i}")) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert stores[0].verify_audit_chain() == (True, 100)
    assert stores[1].verify_audit_chain() == (True, 100)
    for s in stores:
        s.close()


def test_artifact_versions_are_immutable_and_latest_repoints(target: str) -> None:
    store = Store(target)
    store.put_artifact("thing", "k", "1", {"a": 1})
    store.put_artifact("thing", "k", "1", {"a": 1})  # idempotent
    with pytest.raises(ImmutableRecordError):
        store.put_artifact("thing", "k", "1", {"a": 2})
    store.put_latest("run", "A", {"run": "A"})
    store.put_latest("run", "B", {"run": "B"})
    store.put_latest("run", "A", {"run": "A"})
    assert store.get_artifact("run", "latest") == '{"run":"A"}'
    assert [k for k, _, _ in store.list_artifacts("thing")] == ["k"]
    store.close()


@pytest.mark.slow
def test_full_lifecycle_on_postgres_matches_sqlite(
    pg_server: str | None, cfg: Config, demo: DemoResult
) -> None:
    if pg_server is None:
        pytest.skip("no PostgreSQL available")
    result = run_demo(_fresh_database(pg_server), cfg)
    try:
        assert result.evaluation.detection == demo.evaluation.detection
        assert result.evaluation.treatment == demo.evaluation.treatment
        assert result.evaluation.linking == demo.evaluation.linking
        assert result.platform.store.verify_audit_chain()[0]
    finally:
        result.platform.store.close()


def test_least_privilege_roles_cannot_tamper_even_with_raw_sql(
    pg_server: str | None, dataset: SyntheticDataset
) -> None:
    """deploy/postgres/roles.sql: the service role has no UPDATE/DELETE/TRUNCATE and does not
    own the tables, so it cannot drop the append-only triggers either."""
    if pg_server is None:
        pytest.skip("no PostgreSQL available")
    import psycopg

    url = _fresh_database(pg_server)
    database = url.rsplit("/", 1)[1]
    suffix = database[-6:]
    owner, app = f"asas_owner_{suffix}", f"asas_app_{suffix}"
    sql = (Path(__file__).resolve().parents[1] / "deploy" / "postgres" / "roles.sql").read_text(
        encoding="utf-8"
    )
    sql = (
        sql.replace("asas_owner", owner)
        .replace("asas_app", app)
        .replace("asas_reader", f"asas_reader_{suffix}")
        .replace(":'owner_password'", "'o'")
        .replace(":'app_password'", "'a'")
        .replace(":'reader_password'", "'r'")
    )
    code = "\n".join(x for x in sql.splitlines() if not x.strip().startswith("--"))
    with psycopg.connect(url, autocommit=True) as admin:
        for statement in (s.strip() for s in code.split(";")):
            if statement:
                admin.execute(statement)

    def as_role(role: str, password: str) -> str:
        head, tail = url.split("://", 1)
        return f"{head}://{role}:{password}@{tail.split('@', 1)[1]}"

    Store(as_role(owner, "o")).close()  # `asas migrate` as the owner
    service = Store(as_role(app, "a"), auto_migrate=False)
    service.ingest(SourceBundle(trade_events=dataset.bundle.trade_events[:10]))
    service.audit("svc", "DO", "x", {})
    service.put_artifact("thing", "k", "1", {"a": 1})
    with psycopg.connect(as_role(app, "a")) as raw:
        for attack in (
            "UPDATE audit_log SET actor = 'mallory'",
            "DELETE FROM trade_events",
            "TRUNCATE audit_log",
            "DROP TRIGGER audit_log_append_only ON audit_log",
            "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                raw.execute(attack)
            raw.rollback()
    assert service.verify_audit_chain() == (True, 1)
    service.close()


def test_application_role_refuses_a_stale_schema(target: str) -> None:
    with pytest.raises(MigrationError, match="asas migrate"):
        Store(target, auto_migrate=False)  # empty database: the owner has not migrated yet
    Store(target).close()  # `asas migrate`
    Store(target, auto_migrate=False).close()  # current schema: the service starts
