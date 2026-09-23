"""Writes a RunResult to an ASAS-owned directory. Never writes into the source data
location (rail 1). Output is byte-stable for identical inputs."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from asas.fields import DataContractError
from asas.pipeline import RunResult
from asas.serialize import canonical_json, to_jsonable

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection from source-derived values."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _csv(header: list[str], rows: list[list[object]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([[safe_cell(v) for v in row] for row in rows])
    return buf.getvalue()


def _jsonl(records: list[object]) -> str:
    return "".join(json.dumps(to_jsonable(r), sort_keys=True) + "\n" for r in records)


def _guard(out_dir: Path, protected: list[Path]) -> None:
    out = out_dir.resolve()
    for p in protected:
        root = p.resolve()
        if out == root or root in out.parents or out in root.parents:
            raise DataContractError(f"output dir {out} overlaps source location {root}")


def render_files(result: RunResult) -> dict[str, str]:
    cohort_of = {cid: ch.cohort_id for ch in result.cohorts for cid in ch.case_ids}
    rank = {cid: i + 1 for i, cid in enumerate(result.individual_queue)}
    cases = _csv(
        [
            "case_id",
            "trade_id",
            "alert_ids",
            "category",
            "treatment",
            "cohort_id",
            "queue_rank",
            "priority",
            "claims",
            "reasons",
        ],
        [
            [
                c.case_id,
                c.episode.trade_id,
                ";".join(c.episode.alert_ids),
                c.category or "",
                c.treatment.value,
                cohort_of.get(c.case_id, ""),
                rank.get(c.case_id, ""),
                "" if c.priority is None else str(c.priority),
                ";".join(f"{x.claim}={x.verdict.value}" for x in c.claims),
                ";".join(c.reasons),
            ]
            for c in result.cases
        ],
    )
    cohorts = _csv(
        ["cohort_id", "category", "status", "size", "signature", "case_ids"],
        [
            [
                ch.cohort_id,
                ch.category,
                ch.status,
                len(ch.case_ids),
                ";".join(ch.signature),
                ";".join(ch.case_ids),
            ]
            for ch in result.cohorts
        ],
    )
    reports = [
        {"kind": "case", "id": k, "text": v} for k, v in sorted(result.case_reports.items())
    ] + [{"kind": "cohort", "id": k, "text": v} for k, v in sorted(result.cohort_reports.items())]
    drafts = [
        {"case_id": k, "status": "DRAFT_NOT_SENT", "text": v}
        for k, v in sorted(result.rfi_drafts.items())
    ]
    return {
        "decisions.json": canonical_json(result.decisions()) + "\n",
        "cases.csv": cases,
        "cohorts.csv": cohorts,
        "reports.jsonl": _jsonl(list(reports)),
        "rfi_drafts.jsonl": _jsonl(list(drafts)),
        "manifests.jsonl": _jsonl(list(result.manifests)),
    }


def write_run(result: RunResult, out_dir: str | Path, protected: list[Path]) -> list[Path]:
    out = Path(out_dir)
    _guard(out, protected)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in render_files(result).items():
        path = out / name
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written
