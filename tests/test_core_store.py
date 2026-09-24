"""Core infrastructure: config fail-safety, identifiers, tracing, security, append-only store,
tamper-evident audit chain, point-in-time reads, ingestion contract."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from asas.core.config import Config
from asas.core.errors import (
    ConfigMissing,
    DataContractError,
    ImmutableRecordError,
    PermissionDenied,
    UnknownFieldError,
)
from asas.core.ids import canonical_json, fraction_from_hash, stable_id
from asas.core.security import Principal, Role, agent_principal, require_desk, require_role
from asas.core.tracing import InMemoryExporter, OtlpJsonFileExporter, Tracer
from asas.data.ingest import (
    identity_mapping,
    load_csv_bundle,
    mapping_from_dict,
    parse_trade_events,
)
from asas.data.synthetic import SyntheticDataset, write_csv
from asas.store.db import Store


def test_config_missing_keys_fail_loudly(cfg: Config) -> None:
    with pytest.raises(ConfigMissing):
        cfg.without("hypotheses", "price_correction_max_pct").decimal(
            "hypotheses", "price_correction_max_pct"
        )
    with pytest.raises(ConfigMissing):
        cfg.with_value("not-a-number", "scoring", "band_high").decimal("scoring", "band_high")
    assert cfg.version.startswith("asas-config")


def test_identifiers_are_deterministic() -> None:
    assert stable_id("X", "a", "b") == stable_id("X", "a", "b") != stable_id("X", "b", "a")
    assert canonical_json({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'
    assert fraction_from_hash("seed", "case") == fraction_from_hash("seed", "case")


def test_tracing_exports_otlp_json(tmp_path: Path) -> None:
    memory = InMemoryExporter()
    path = tmp_path / "spans.jsonl"
    tracer = Tracer([memory, OtlpJsonFileExporter(path)])
    with tracer.span("outer", k="v"), tracer.span("inner"):
        pass
    inner, outer = memory.spans
    assert inner.parent_span_id == outer.span_id and inner.trace_id == outer.trace_id
    doc = json.loads(path.read_text().splitlines()[0])
    span = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16


def test_roles_and_entitlements() -> None:
    analyst = Principal(user_id="a", roles=frozenset({Role.INVESTIGATOR}), desks=frozenset({"FX"}))
    with pytest.raises(PermissionDenied):
        require_role(analyst, Role.APPROVER)
    with pytest.raises(PermissionDenied):
        require_desk(analyst, "EQ")
    agent = agent_principal("investigator", analyst)
    assert agent.user_id == "agent:investigator" and not agent.has(Role.APPROVER)
    assert agent.desks == analyst.desks


def test_store_is_append_only_and_audit_is_tamper_evident(db_path: Path) -> None:
    store = Store(db_path)
    store.put_artifact("thing", "k", "1", {"a": 1})
    store.put_artifact("thing", "k", "1", {"a": 1})  # idempotent
    with pytest.raises(ImmutableRecordError):
        store.put_artifact("thing", "k", "1", {"a": 2})
    store.audit("alice", "DO", "x", {"n": 1})
    store.audit("bob", "DO", "y", {"n": 2})
    assert store.verify_audit_chain() == (True, 2)
    with pytest.raises(ImmutableRecordError), store.writer() as conn:
        conn.execute("UPDATE audit_log SET actor = 'mallory'")
    store.close()
    raw = sqlite3.connect(db_path)  # an attacker bypassing the application
    raw.execute("DROP TRIGGER audit_log_no_update")
    raw.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 1")
    raw.commit()
    raw.close()
    assert Store(db_path).verify_audit_chain()[0] is False


def test_reader_connection_is_read_only(db_path: Path) -> None:
    store = Store(db_path)
    with store.reader() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO spans VALUES ('s','t',NULL,'n',0,0,'OK','{}')")
    with pytest.raises(PermissionError):
        store.query("DELETE FROM spans")


def test_point_in_time_reads(db_path: Path, dataset: SyntheticDataset) -> None:
    store = Store(db_path)
    store.ingest(dataset.bundle)
    cut = dataset.end - timedelta(days=30)
    snapshot = store.load_bundle(cut)
    assert snapshot.trade_events and all(e.record_time <= cut for e in snapshot.trade_events)
    assert all(a.record_time <= cut for a in snapshot.alerts)
    assert all(o.decided_at < cut for o in snapshot.outcomes)
    assert len(snapshot.trade_events) < len(dataset.bundle.trade_events)


def test_ingest_is_idempotent_and_rejects_in_place_changes(
    db_path: Path, dataset: SyntheticDataset
) -> None:
    store = Store(db_path)
    first = store.ingest(dataset.bundle)
    second = store.ingest(dataset.bundle)
    assert first["trade_events"] > 0 and second["trade_events"] == 0
    tampered = dataset.bundle.trade_events[0].model_copy(update={"book": "OTHER"})
    from asas.data.ingest import SourceBundle

    with pytest.raises(DataContractError):
        store.ingest(SourceBundle(trade_events=(tampered,)))


def test_csv_roundtrip_through_identity_mapping(tmp_path: Path, dataset: SyntheticDataset) -> None:
    write_csv(dataset, tmp_path / "csv")
    bundle = load_csv_bundle(tmp_path / "csv", identity_mapping())
    assert len(bundle.trade_events) == len(dataset.bundle.trade_events)
    assert len(bundle.alerts) == len(dataset.bundle.alerts)
    assert {a.workflow.get("ALERT_GRP_ID") for a in bundle.alert_annexes} - {None}


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ({"EVENT_TIME": "2026-01-05T09:00:00"}, "naive timestamp"),
        ({"PRICE": "NaN"}, "non-finite"),
        ({"EVENT_TYPE": "DELETE"}, "unknown value"),
        ({"TRADE_ID": " "}, "required"),
    ],
)
def test_dirty_rows_fail_loudly(row: dict[str, str], error: str) -> None:
    base = {
        "TRADE_ID": "T1",
        "TRADE_VERSION": "1",
        "EVENT_TYPE": "NEW",
        "EVENT_TIME": "2026-01-05T09:00:00+00:00",
        "RECORD_TIME": "2026-01-05T09:01:00+00:00",
        "BOOK": "B",
        "DESK": "D",
        "INSTRUMENT_ID": "I",
        "PRODUCT_TYPE": "P",
        "CURRENCY": "USD",
        "SOURCE": "CAL",
        "PRICE": "1",
        "QUANTITY": "1",
    }
    with pytest.raises(DataContractError, match=error):
        parse_trade_events([{**base, **row}])


def test_mapping_rejects_unknown_columns_and_fields() -> None:
    with pytest.raises(DataContractError, match="not in the contract"):
        mapping_from_dict(
            {"version": "v", "trade_events": {"columns": {"X": "RISK"}}, "alerts": {"columns": {}}}
        )
    mapping = identity_mapping()
    with pytest.raises(UnknownFieldError):
        mapping.entities["alerts"].to_contract({"ALERT_ID": "A", "SURPRISE_COLUMN": "1"})
