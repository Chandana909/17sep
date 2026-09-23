"""Phase 1: mapping + CSV/SQL adapters (rails 1, 10, 16)."""

from __future__ import annotations

import csv
import shutil
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from asas import load_config, run
from asas.adapters.csv_source import csv_source
from asas.adapters.sql_source import SqlReader, sql_source, sqlite_readonly
from asas.fields import DataContractError, UnknownFieldError
from asas.mapping import SourceMapping, load_mapping, mapping_from_dict
from asas.models import Treatment
from factories import ROOT

SAMPLE = ROOT / "data" / "sample"
MAPPING = ROOT / "config" / "mapping.sample.toml"
AS_OF = datetime(2026, 1, 12, tzinfo=UTC)


def _sample_run(source):  # type: ignore[no-untyped-def]
    return run(source, load_config(ROOT / "config" / "asas.v1.toml"), AS_OF)


def _by_trade(result):  # type: ignore[no-untyped-def]
    return {c.episode.trade_id: c for c in result.cases}


def test_sample_csv_golden() -> None:
    cases = _by_trade(_sample_run(csv_source(SAMPLE, load_mapping(MAPPING))))
    assert set(cases) == {"T1001", "T1002", "T1003", "T1004", "T1006", "T1007"}  # A7 is future
    assert cases["T1001"].treatment is Treatment.BULK_CANDIDATE
    assert cases["T1002"].treatment is Treatment.BULK_CANDIDATE  # injection text is inert
    assert "OPEN_RFI" in cases["T1007"].reasons
    assert cases["T1004"].claims[0].verdict.value == "VERIFIED"  # rebook via ORIGINAL_TRADE_ID
    assert "RFI_FLAG" not in repr(cases["T1001"])


def _to_sqlite(db: Path) -> None:
    conn = sqlite3.connect(db)
    for path in SAMPLE.glob("*.csv"):
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        cols = ", ".join(f'"{c}"' for c in rows[0])
        conn.execute(f'CREATE TABLE "{path.stem}" ({cols})')
        marks = ", ".join("?" for _ in rows[0])
        conn.executemany(f'INSERT INTO "{path.stem}" VALUES ({marks})', rows[1:])
    conn.commit()
    conn.close()


def _sql_mapping() -> SourceMapping:
    text = MAPPING.read_text(encoding="utf-8")
    for path in SAMPLE.glob("*.csv"):
        text = text.replace(f'"{path.name}"', f'"{path.stem}"')
    return mapping_from_dict(tomllib.loads(text))


def test_sqlite_matches_csv_and_issues_only_pit_selects(tmp_path: Path) -> None:
    db = tmp_path / "scp.db"
    _to_sqlite(db)
    statements: list[str] = []
    base = sqlite_readonly(db)

    def traced() -> sqlite3.Connection:
        conn = base()
        conn.set_trace_callback(statements.append)
        return conn

    via_sql = _sample_run(sql_source(traced, _sql_mapping()))
    via_csv = _sample_run(csv_source(SAMPLE, load_mapping(MAPPING)))
    assert via_sql.decisions_json() == via_csv.decisions_json()
    executed = [s for s in statements if not s.startswith("PRAGMA")]
    assert executed and all(s.lstrip().upper().startswith("SELECT") for s in executed)
    data_reads = [s for s in executed if "WHERE 1 = 0" not in s]
    assert len(data_reads) == 4
    assert all('"CREATED_AT" <=' in s or '"DECIDED_AT" <' in s for s in data_reads)


def test_sqlite_connection_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("CREATE TABLE t (a)").connection.commit()
    conn = sqlite_readonly(db)()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (1)")


@pytest.mark.parametrize("ident", ["t; DROP TABLE x", 't"--', "1abc", "a b"])
def test_sql_identifier_injection_rejected(ident: str) -> None:
    reader = SqlReader(lambda: None, load_mapping(MAPPING).sql)
    with pytest.raises(DataContractError):
        reader.quote(ident)


def test_schema_drift_fails_loudly(tmp_path: Path) -> None:
    data = tmp_path / "d"
    shutil.copytree(SAMPLE, data)
    path = data / "scp_alerts.csv"
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    rows[0].append("NEW_RISK_SCORE")
    for r in rows[1:]:
        r.append("9")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)
    with pytest.raises(UnknownFieldError) as exc:
        _sample_run(csv_source(data, load_mapping(MAPPING)))
    assert exc.value.fields == ["NEW_RISK_SCORE"]


def test_unmapped_rfi_source_blocks_bulk_by_default() -> None:
    text = MAPPING.read_text(encoding="utf-8")
    cut = text[: text.index("[rfi_events]")] + text[text.index("[past_cases]") :]
    mapping = mapping_from_dict(tomllib.loads(cut))
    result = _sample_run(csv_source(SAMPLE, mapping))
    assert result.cohorts == ()
    assert all("DATA_UNAVAILABLE:rfi_events" in c.reasons for c in result.cases)
    assert result.data_unavailable == ("rfi_events",)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"alerts": {"source": "a.csv", "columns": {"X": "RISK"}}}, "not in the contract"),
        ({"alerts": {"source": "a.csv", "columns": {"X": "ALERT_ID", "Y": "ALERT_ID"}}}, "several"),
        ({"alerts": {"source": "a.csv", "naive_timestamp_offset": "UTC"}}, "look like"),
        ({"alerts": {"source": "a.csv", "transform": "os.system; rm"}}, "module:function"),
        ({"bogus": {}}, "unknown mapping sections"),
        ({"sql": {"paramstyle": "weird"}}, "paramstyle"),
    ],
)
def test_bad_mappings_rejected(patch: dict[str, object], message: str) -> None:
    with pytest.raises(DataContractError, match=message):
        mapping_from_dict({"version": "t", **patch})


def test_value_maps_timestamp_format_and_transform_hook() -> None:
    mapping = mapping_from_dict(
        {
            "version": "t",
            "trade_events": {
                "source": "x",
                "timestamp_format": "%d/%m/%Y %H:%M",
                "naive_timestamp_offset": "+01:00",
                "transform": "hooks_for_tests:upper_side",
                "columns": {"S": "SIDE", "T": "EVENT_TIME", "ID": "TRADE_ID"},
                "values": {"SIDE": {"B": "BUY"}},
            },
        }
    )
    row = mapping.entities["trade_events"].to_contract({"S": "b", "T": "05/01/2026 10:00", "ID": 7})
    assert row["SIDE"] == "BUY"
    assert row["EVENT_TIME"].isoformat() == "2026-01-05T10:00:00+01:00"  # type: ignore[attr-defined]
    assert row["TRADE_ID"] == "7"
