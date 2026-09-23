"""Phase 2: CLI and output surface."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from asas.cli import draft_mapping, main
from asas.fields import DataContractError
from asas.mapping import mapping_from_dict
from asas.output import safe_cell
from factories import ROOT

SAMPLE = str(ROOT / "data" / "sample")
MAPPING = str(ROOT / "config" / "mapping.sample.toml")
CONFIG = str(ROOT / "config" / "asas.v1.toml")
AS_OF = "2026-01-12T00:00:00+00:00"


def _run(out: Path, *extra: str) -> int:
    return main(
        [
            "run",
            "--data",
            SAMPLE,
            "--mapping",
            MAPPING,
            "--config",
            CONFIG,
            "--as-of",
            AS_OF,
            "--out",
            str(out),
            *extra,
        ]
    )


def test_run_is_byte_stable(tmp_path: Path) -> None:
    assert _run(tmp_path / "a") == 0
    assert _run(tmp_path / "b") == 0
    names = sorted(p.name for p in (tmp_path / "a").iterdir())
    assert names == [
        "cases.csv",
        "cohorts.csv",
        "decisions.json",
        "manifests.jsonl",
        "reports.jsonl",
        "rfi_drafts.jsonl",
    ]
    for name in names:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    drafts = (tmp_path / "a" / "rfi_drafts.jsonl").read_text(encoding="utf-8")
    assert "DRAFT_NOT_SENT" in drafts
    assert "trader.a" not in (tmp_path / "a" / "reports.jsonl").read_text(encoding="utf-8")


def test_entitled_run_shows_people_but_same_decisions(tmp_path: Path) -> None:
    _run(tmp_path / "a")
    _run(tmp_path / "b", "--entitled")
    assert (tmp_path / "a" / "decisions.json").read_bytes() == (
        tmp_path / "b" / "decisions.json"
    ).read_bytes()
    assert "trader.a" in (tmp_path / "b" / "reports.jsonl").read_text(encoding="utf-8")


def test_refuses_to_write_into_source_location(tmp_path: Path) -> None:
    data = tmp_path / "data"
    shutil.copytree(SAMPLE, data)
    code = main(
        [
            "run",
            "--data",
            str(data),
            "--mapping",
            MAPPING,
            "--config",
            CONFIG,
            "--as-of",
            AS_OF,
            "--out",
            str(data / "out"),
        ]
    )
    assert code == 2


def test_naive_as_of_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["run", "--data", SAMPLE, "--mapping", MAPPING, "--as-of", "2026-01-12", "--out", "x"])


def test_check_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--config", CONFIG]) == 0
    assert main(["check-mapping", "--data", SAMPLE, "--mapping", MAPPING, "--as-of", AS_OF]) == 0
    assert "mapping OK" in capsys.readouterr().out


def test_init_mapping_drafts_a_loadable_mapping() -> None:
    import tomllib

    draft = tomllib.loads(draft_mapping(SAMPLE))
    mapping = mapping_from_dict(draft)
    assert mapping.entities["alerts"].source == "scp_alerts.csv"
    assert mapping.entities["alerts"].columns["ALERT_DATE"] == "ALERT_TIME"
    assert mapping.entities["trade_events"].source == "trade_versions.csv"


@pytest.mark.parametrize("value", ["=HYPERLINK(1)", "+1", "-2", "@x"])
def test_csv_formula_injection_neutralised(value: str) -> None:
    assert safe_cell(value).startswith("'")


def test_data_contract_error_is_exit_code_2(tmp_path: Path) -> None:
    bad = tmp_path / "m.toml"
    bad.write_text('version = "x"\n[alerts]\nsource = "a.csv"\ncolumns = {X = "NOPE"}\n')
    with pytest.raises(DataContractError):
        mapping_from_dict({"version": "x", "alerts": {"source": "a", "columns": {"X": "NOPE"}}})
    assert main(["check-mapping", "--data", SAMPLE, "--mapping", str(bad)]) == 2
