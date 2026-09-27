"""Real-data integration path: readers, mapping transforms, collected validation, draft
mapping from vendor headers, and the capability matrix. This is the loop a person or a small
coding model runs when real SCP/CAL extracts arrive."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from asas.cli import main
from asas.core.config import Config
from asas.core.errors import DataContractError
from asas.data.ingest import (
    load_mapping,
    load_source_bundle,
    mapping_from_dict,
    process_source,
)
from asas.data.profiling import draft_mapping, guess_timestamp_format
from asas.data.readers import read_rows
from asas.data.synthetic import SyntheticDataset, write_vendor_extract
from asas.engine.rules import load_ruleset
from asas.services.demo import ruleset_path
from asas.services.integration import capability_matrix

TRADE_COLUMNS = {
    "ID": "TRADE_ID",
    "V": "TRADE_VERSION",
    "T": "EVENT_TYPE",
    "EXEC": "EVENT_TIME",
    "BOOK": "BOOK",
    "DESK": "DESK",
    "INS": "INSTRUMENT_ID",
    "PROD": "PRODUCT_TYPE",
    "PX": "PRICE",
}


@pytest.fixture(scope="module")
def vendor(tmp_path_factory: pytest.TempPathFactory, dataset: SyntheticDataset) -> Iterator[Path]:
    out = tmp_path_factory.mktemp("vendor")
    write_vendor_extract(dataset, out)
    yield out


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header), *(",".join(r) for r in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _minimal_alerts(root: Path) -> dict[str, object]:
    _write(
        root / "alerts.csv",
        ["ALERT_ID", "RULE_ID", "SUBRULE_ID", "ALERT_TIME", "RECORD_TIME", "TRADE_ID", "BOOK",
         "DESK", "INSTRUMENT_ID"],
        [["A1", "R100", "R100.1", "2026-01-05T10:00:00+00:00", "2026-01-05T10:00:00+00:00",
          "T1", "B", "EQ", "I"]],
    )  # fmt: skip
    return {"source": "alerts.csv", "columns": {c: c for c in (
        "ALERT_ID", "RULE_ID", "SUBRULE_ID", "ALERT_TIME", "RECORD_TIME", "TRADE_ID", "BOOK",
        "DESK", "INSTRUMENT_ID")}}  # fmt: skip


# ---------------------------------------------------------------- the weak-model loop


def test_draft_mapping_from_vendor_headers_loads_the_same_data(
    vendor: Path, dataset: SyntheticDataset, tmp_path: Path
) -> None:
    draft = draft_mapping(vendor)
    assert "# REVIEW" in draft  # guesses are always marked for review
    path = tmp_path / "mapping.toml"
    path.write_text(draft, encoding="utf-8")
    report, bundle = process_source(vendor, load_mapping(path))
    assert report.ok, report.render()
    ignored = load_mapping(path).entities["trade_events"].ignore
    assert "COST_CENTER" in ignored

    def key(e):  # type: ignore[no-untyped-def]
        return (e.trade_id, e.version, e.event_type, e.price, e.quantity, e.side, e.book)

    assert {key(e) for e in bundle.trade_events} == {key(e) for e in dataset.bundle.trade_events}
    assert {e.event_time.replace(microsecond=0) for e in bundle.trade_events} == {
        e.event_time.replace(microsecond=0) for e in dataset.bundle.trade_events
    }
    assert {a.alert_id for a in bundle.alerts} == {a.alert_id for a in dataset.bundle.alerts}
    assert {(o.outcome_id, o.label, o.quality) for o in bundle.outcomes} == {
        (o.outcome_id, o.label, o.quality) for o in dataset.bundle.outcomes
    }
    assert len(bundle.trade_persons) == len(dataset.bundle.trade_persons)
    assert {x.workflow.get("ALERT_GRP_ID") for x in bundle.alert_annexes} - {None}


def test_check_reports_each_distinct_problem_once_with_a_fix(
    vendor: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = draft_mapping(vendor)
    broken = "\n".join(
        line
        for line in draft.splitlines()
        if not line.startswith(("N = ", "timestamp_format", "DISPOSITION_ID"))
    )
    path = tmp_path / "broken.toml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(DataContractError, match="OUTCOME_ID"):
        load_mapping(path)  # a required field lost its column: refused before reading data
    fixed = broken.replace(
        "[outcomes.columns]\n", '[outcomes.columns]\nDISPOSITION_ID = "OUTCOME_ID"\n'
    )
    path.write_text(fixed, encoding="utf-8")
    code = main(["data", "check", "--data", str(vendor), "--mapping", str(path), "--json"])
    assert code == 1
    report = json.loads(capsys.readouterr().out)["validation"]
    issues = report["entities"]["trade_events"]["issues"]
    messages = {i["message"]: i for i in issues}
    assert "EVENT_TYPE: unknown value 'N'" in messages
    assert "values.EVENT_TYPE" in messages["EVENT_TYPE: unknown value 'N'"]["hint"]
    assert any("timestamp_format" in i["hint"] for i in issues)
    assert sum(i["count"] for i in issues) == report["entities"]["trade_events"]["failed"]
    assert all(len(i["rows"]) <= 3 for i in issues)


def test_strict_loader_refuses_any_failure(vendor: Path, tmp_path: Path) -> None:
    draft = draft_mapping(vendor).replace('B = "BUY"', "")
    path = tmp_path / "m.toml"
    path.write_text(draft, encoding="utf-8")
    with pytest.raises(DataContractError, match="data check"):
        load_source_bundle(vendor, load_mapping(path))


def test_cli_draft_refuses_to_overwrite(vendor: Path, tmp_path: Path) -> None:
    out = tmp_path / "m.toml"
    assert main(["data", "draft-mapping", "--data", str(vendor), "--out", str(out)]) == 0
    assert main(["data", "draft-mapping", "--data", str(vendor), "--out", str(out)]) == 2
    assert main(["data", "profile", "--data", str(vendor)]) == 0


# ---------------------------------------------------------------- mapping transforms


def test_constants_copy_timezone_and_decimal_comma(tmp_path: Path) -> None:
    _write(
        tmp_path / "trades.csv",
        [*TRADE_COLUMNS, "EXTRA"],
        [
            ["T1", "1", "NEW", "15/01/2026 09:00:00", "B", "EQ", "I", "P", "101.5"],
            ["T2", "1", "NEW", "15/07/2026 09:00:00", "B", "EQ", "I", "P", "7"],
        ],
    )
    (tmp_path / "trades.csv").write_text(
        (tmp_path / "trades.csv").read_text(encoding="utf-8").replace("101.5", '"101,5"'),
        encoding="utf-8",
    )
    mapping = mapping_from_dict(
        {
            "version": "t",
            "trade_events": {
                "source": "trades.csv",
                "columns": TRADE_COLUMNS,
                "ignore": ["EXTRA"],
                "constants": {"CURRENCY": "USD", "SOURCE": "SCP"},
                "copy": {"RECORD_TIME": "EVENT_TIME"},
                "timestamp_format": "%d/%m/%Y %H:%M:%S",
                "timezone": "Europe/London",
                "decimal_comma": True,
            },
            "alerts": _minimal_alerts(tmp_path),
        }
    )
    bundle = load_source_bundle(tmp_path, mapping)
    winter, summer = sorted(bundle.trade_events, key=lambda e: e.trade_id)
    assert winter.event_time == datetime(2026, 1, 15, 9, tzinfo=UTC)  # GMT
    assert summer.event_time == datetime(2026, 7, 15, 8, tzinfo=UTC)  # BST is UTC+1
    assert winter.record_time == winter.event_time  # copied, per the mapping
    assert winter.price == Decimal("101.5") and winter.currency == "USD"
    assert winter.source == "SCP"


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"surprise": 1}, "unknown mapping keys"),
        ({"timezone": "Mars/Olympus"}, "unknown timezone"),
        ({"timezone": "UTC", "naive_timestamp_offset": "+00:00"}, "not both"),
        ({"constants": {"RISK": "x"}}, "not in the contract"),
    ],
)
def test_mapping_mistakes_fail_before_any_data_is_read(
    tmp_path: Path, patch: dict[str, object], error: str
) -> None:
    spec = {"source": "t.csv", "columns": TRADE_COLUMNS, **patch}
    spec.setdefault("constants", {"CURRENCY": "USD", "SOURCE": "S", "RECORD_TIME": "x"})
    with pytest.raises(DataContractError, match=error):
        mapping_from_dict(
            {"version": "t", "trade_events": spec, "alerts": _minimal_alerts(tmp_path)}
        )


def test_readers_explain_what_to_do(tmp_path: Path) -> None:
    (tmp_path / "x.dat").write_text("a", encoding="utf-8")
    with pytest.raises(DataContractError, match="set `reader`"):
        read_rows(tmp_path / "x.dat")
    (tmp_path / "bad.jsonl").write_text('{"a": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(DataContractError, match=r"bad.jsonl:2"):
        read_rows(tmp_path / "bad.jsonl")
    (tmp_path / "wide.csv").write_text("A,B\n1,2,3\n", encoding="utf-8")
    with pytest.raises(DataContractError, match="more cells than the header"):
        read_rows(tmp_path / "wide.csv")
    assert guess_timestamp_format(["31/01/2026 10:00:00"]) == "%d/%m/%Y %H:%M:%S"


# ---------------------------------------------------------------- capabilities


def test_capability_matrix_names_what_degrades(cfg: Config, dataset: SyntheticDataset) -> None:
    rules = load_ruleset(ruleset_path(), cfg).rules
    full = capability_matrix(dataset.bundle, cfg, rules)
    assert set(full["hypotheses"].values()) == {"ENABLED"} and full["rule_gaps"] == []
    no_price = cfg.with_value(["PRICE"], "data", "unavailable_fields")
    degraded = capability_matrix(dataset.bundle, no_price, rules)
    assert degraded["hypotheses"]["OFF_MARKET_AMENDMENT"].startswith("DEGRADED")
    assert degraded["hypotheses"]["LATE_BOOKING_OPERATIONAL"] == "ENABLED"
    assert degraded["deviation_signals"]["max_price_change_pct"].startswith("UNAVAILABLE")
    assert degraded["rule_gaps"] == ["R100.1:PRICE"]
