"""Command line.

asas demo      --db out/asas.db [--days 120] [--report out/evaluation.md]
asas serve     --db out/asas.db [--host 127.0.0.1] [--port 8000]
asas generate  --out data/synthetic [--days 120]
asas ingest    --db out/asas.db --data <dir> [--mapping config/mapping.example.toml]
asas pipeline  --db out/asas.db [--as-of ISO]
asas challenge | discover --db out/asas.db [--as-of ISO]
asas verify-audit --db out/asas.db
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from asas.core.config import default_config_path, load_config
from asas.core.errors import AsasError
from asas.core.logging import configure_logging
from asas.core.security import SYSTEM
from asas.data.ingest import identity_mapping, load_csv_bundle, load_mapping
from asas.data.synthetic import GeneratorSpec, generate, write_csv
from asas.services.demo import ADMIN, ANALYST, ruleset_path, run_demo
from asas.services.evaluation import render_markdown
from asas.services.platform import Platform, build_gateway
from asas.store.db import Store


def _as_of(text: str | None, platform: Platform) -> datetime:
    if text:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            raise argparse.ArgumentTypeError("--as-of needs a timezone, e.g. +00:00")
        return value
    run = platform.latest_run()
    if run is not None:
        return run.as_of
    watermark = platform.store.data_watermark()
    if watermark is None:
        raise AsasError("no data ingested")
    return watermark


def _platform(args: argparse.Namespace) -> Platform:
    cfg = load_config(args.config)
    store_path = Path(args.db)
    gateway = build_gateway(cfg, Store(store_path)) if cfg.boolean("agents", "enabled") else None
    platform = Platform(store_path, cfg, gateway)
    platform.seed_policy(ruleset_path())
    return platform


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db = Path(args.db)
    if db.exists():
        print(f"refusing to overwrite existing store {db}; choose a new --db path", file=sys.stderr)
        return 2
    result = run_demo(db, cfg, GeneratorSpec(days=args.days, seed=args.seed))
    markdown = render_markdown(result.evaluation)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(markdown, encoding="utf-8")
    print(markdown)
    print(
        f"first run: {result.first_run.cases} cases, {result.first_run.proposed_bulk} bulk, "
        f"{result.first_run.escalation} escalations"
    )
    print(f"agent-verified links confirmed by analyst: {len(result.confirmed_links)}")
    print(
        f"challenger findings: {len(result.challenge.findings)}; "
        f"patterns: {len(result.discovery.patterns)}; candidates: "
        + ", ".join(f"{k}={v.value}" for k, v in result.candidate_states.items())
    )
    print(f"released bundles: {', '.join(b.bundle_id for b in result.released) or 'none'}")
    print(
        f"final run: {result.final_run.cases} cases, {result.final_run.proposed_bulk} bulk, "
        f"{result.final_run.escalation} escalations, {result.final_run.cohorts} cohorts"
    )
    ok, entries = result.platform.store.verify_audit_chain()
    print(f"audit chain valid={ok} entries={entries}")
    print(f"serve the console with: asas serve --db {db}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from asas.api.app import create_app

    configure_logging()
    uvicorn.run(create_app(_platform(args)), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    dataset = generate(GeneratorSpec(days=args.days, seed=args.seed))
    write_csv(dataset, args.out)
    print(f"wrote synthetic SCP/CAL files to {args.out}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    platform = _platform(args)
    mapping = load_mapping(args.mapping) if args.mapping else identity_mapping()
    counts = platform.ingest(load_csv_bundle(args.data, mapping), ADMIN)
    print(json.dumps(counts, indent=1))
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    platform = _platform(args)
    report = platform.run_pipeline(_as_of(args.as_of, platform), SYSTEM)
    print(report.model_dump_json(indent=1, exclude={"case_ids", "cohort_list"}))
    return 0


def cmd_challenge(args: argparse.Namespace) -> int:
    platform = _platform(args)
    report = platform.challenge(_as_of(args.as_of, platform), ANALYST)
    for f in report.findings:
        print(f"{f.kind.value:<26} {f.status:<16} {f.episode_id} {f.summary}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    platform = _platform(args)
    report = platform.discover(_as_of(args.as_of, platform), ANALYST)
    for c in report.candidates:
        print(f"{c.candidate_id} {c.kind.value} {c.rule.conditions if c.rule else c.bulk_scope}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ok, entries = Store(args.db).verify_audit_chain()
    print(f"audit chain valid={ok} entries={entries}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asas", description="Auditable agentic surveillance")
    parser.add_argument("--config", default=str(default_config_path()))
    sub = parser.add_subparsers(dest="command", required=True)

    def with_db(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--db", required=True, help="path of the ASAS store (SQLite)")
        return p

    p = with_db(sub.add_parser("demo", help="run the full lifecycle on synthetic data"))
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--report", help="write the evaluation markdown here")
    p.set_defaults(func=cmd_demo)

    p = with_db(sub.add_parser("serve", help="serve the API and console"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("generate", help="write synthetic SCP/CAL CSV files")
    p.add_argument("--out", required=True)
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_generate)

    p = with_db(sub.add_parser("ingest", help="ingest CSV files through a versioned mapping"))
    p.add_argument("--data", required=True)
    p.add_argument("--mapping")
    p.set_defaults(func=cmd_ingest)

    for name, fn in (
        ("pipeline", cmd_pipeline),
        ("challenge", cmd_challenge),
        ("discover", cmd_discover),
    ):
        p = with_db(sub.add_parser(name))
        p.add_argument("--as-of")
        p.set_defaults(func=fn)

    p = with_db(sub.add_parser("verify-audit", help="verify the hash-chained audit log"))
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AsasError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
