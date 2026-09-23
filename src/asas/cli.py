"""Command line: run | check-mapping | check-config | init-mapping.

    python -m asas run --data data/sample --mapping config/mapping.sample.toml \\
        --as-of 2026-01-12T00:00:00+00:00 --out out/
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from asas.adapters.csv_source import CsvReader, csv_source
from asas.adapters.gateways import build_gateway
from asas.adapters.mapped import MappedSource
from asas.adapters.replay import RecordingGateway
from asas.adapters.sql_source import sql_source, sqlite_readonly
from asas.config import load_config
from asas.config_check import validate_config
from asas.fields import ENTITY_FIELDS, REQUIRED_FIELDS, DataContractError
from asas.mapping import ENTITIES, load_mapping
from asas.output import write_run
from asas.pipeline import run

DEFAULT_CONFIG = "config/asas.v1.toml"

# Known SCP column names (design Appendix A) -> contract field, used only to *draft* a mapping.
SUGGESTIONS: dict[str, dict[str, str]] = {
    "alerts": {
        "ALERT_ID": "ALERT_ID",
        "ALERT_TYPE_ID": "ALERT_TYPE",
        "ALERT_DATE": "ALERT_TIME",
        "CREATED_AT": "RECORD_TIME",
        "TRADE_ID": "TRADE_ID",
        "INSTRUMENT_IDENTIFIER": "INSTRUMENT_ID",
        "BOOK": "BOOK_ID",
        "REASON_COMMENT": "EXPLANATION_TEXT",
    },
    "trade_events": {
        "TRADE_ID": "TRADE_ID",
        "EVENT_SUB_TYPE_ID": "EVENT_TYPE",
        "TRADE_DATE_TIME": "EVENT_TIME",
        "CREATED_AT": "RECORD_TIME",
        "INSTRUMENT_IDENTIFIER": "INSTRUMENT_ID",
        "BOOK": "BOOK_ID",
        "BUY_OR_SELL": "SIDE",
    },
    "rfi_events": {"CREATED_AT": "RECORD_TIME", "ACTION": "RFI_ACTION"},
    "past_cases": {},
}


def _as_of(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of needs a timezone, e.g. +00:00")
    return value


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mapping", required=True, help="source mapping TOML")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--data", help="directory of CSV files")
    group.add_argument("--sqlite", help="SQLite database file (opened read-only)")
    group.add_argument("--db-factory", help="module:function returning a DB-API connection")


def _source(args: argparse.Namespace) -> tuple[MappedSource, list[Path]]:
    mapping = load_mapping(args.mapping)
    if args.data:
        return csv_source(args.data, mapping), [Path(args.data)]
    if args.sqlite:
        return sql_source(sqlite_readonly(args.sqlite), mapping), [Path(args.sqlite)]
    module, func = args.db_factory.split(":")
    return sql_source(getattr(importlib.import_module(module), func), mapping), []


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    source, protected = _source(args)
    gateway = build_gateway(cfg)
    recorder = RecordingGateway(gateway) if gateway is not None and args.record_llm else None
    result = run(source, cfg, args.as_of, gateway=recorder or gateway, entitled=args.entitled)
    for path in write_run(result, args.out, [*protected, Path(args.mapping).parent]):
        print(f"wrote {path}")
    if recorder is not None:
        recorder.save(args.record_llm)
    bulk = sum(len(c.case_ids) for c in result.cohorts)
    print(
        f"cases={len(result.cases)} cohorts={len(result.cohorts)} proposed_bulk={bulk} "
        f"individual={len(result.individual_queue)}"
    )
    return 0


def cmd_check_mapping(args: argparse.Namespace) -> int:
    mapping = load_mapping(args.mapping)
    problems: list[str] = []
    for entity in ENTITIES:
        em = mapping.entities.get(entity)
        if em is None:
            print(f"{entity}: not mapped (treated as unavailable)")
            continue
        if em.missing_required:
            problems.append(f"{entity}: required fields not mapped {sorted(em.missing_required)}")
    source, _ = _source(args)
    problems += source.schema_problems()
    try:
        as_of = args.as_of or datetime.now().astimezone()
        for entity in ENTITIES:
            if entity in mapping.entities:
                count = len(getattr(source, entity)(as_of))
                print(f"{entity}: {count} rows parsed OK as of {as_of.isoformat()}")
    except DataContractError as exc:
        problems.append(f"parse error: {exc}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    print("mapping OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_check_config(args: argparse.Namespace) -> int:
    problems = validate_config(load_config(args.config))
    for problem in problems:
        print(f"PROBLEM {problem}")
    print("config OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def draft_mapping(data_dir: str | Path) -> str:
    reader = CsvReader(data_dir)
    lines = [
        "# DRAFT generated by `asas init-mapping`. Review every line, then run check-mapping.",
        'version = "mapping-draft-1"',
        "",
    ]
    for path in sorted(Path(data_dir).glob("*.csv")):
        header = reader.columns(path.name)

        def score(entity: str, header: Sequence[str] = header) -> int:
            fields = {SUGGESTIONS[entity].get(c, c) for c in header}
            return len(fields & REQUIRED_FIELDS[entity])

        entity = max(ENTITIES, key=score)
        if score(entity) == 0:
            lines += [f"# {path.name}: could not match an entity; columns: {list(header)}", ""]
            continue
        mapped: list[tuple[str, str]] = []
        ignored: list[tuple[str, str]] = []
        for col in header:
            target = SUGGESTIONS[entity].get(col, col)
            (mapped if target in ENTITY_FIELDS[entity] else ignored).append((col, target))
        lines += [
            f"[{entity}]",
            f'source = "{path.name}"',
            '# naive_timestamp_offset = "+00:00"',
            f"ignore = {[c for c, _ in ignored]}  # TODO review",
            "",
            f"[{entity}.columns]",
        ]
        lines += [f'{c} = "{t}"' for c, t in mapped]
        missing = sorted(REQUIRED_FIELDS[entity] - {t for _, t in mapped})
        if missing:
            lines.append(f"# TODO map columns for required fields: {missing}")
        lines.append("")
    return "\n".join(lines)


def cmd_init_mapping(args: argparse.Namespace) -> int:
    text = draft_mapping(args.data)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asas", description="ASAS bulk-review preparation")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="produce cases, cohort proposals and reports")
    _add_source_args(p)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=_as_of, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--entitled", action="store_true", help="include person fields in reports")
    p.add_argument("--record-llm", help="save model responses for deterministic replay")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check-mapping", help="validate a mapping against the data")
    _add_source_args(p)
    p.add_argument("--as-of", type=_as_of)
    p.set_defaults(func=cmd_check_mapping)

    p = sub.add_parser("check-config", help="validate a decision config")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("init-mapping", help="draft a mapping from CSV headers")
    p.add_argument("--data", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_init_mapping)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DataContractError as exc:
        print(f"DATA CONTRACT ERROR: {exc}", file=sys.stderr)
        return 2
