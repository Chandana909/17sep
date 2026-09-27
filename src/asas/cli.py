"""Command line.

asas demo      --db out/asas.db [--days 120] [--report out/evaluation.md]
asas serve     --db out/asas.db [--host 127.0.0.1] [--port 8000]
asas generate  --out data/synthetic [--days 120] [--style contract|vendor]
asas ingest    --db out/asas.db --data <dir> [--mapping config/mapping.example.toml]
asas pipeline  --db out/asas.db [--as-of ISO]
asas challenge | discover --db out/asas.db [--as-of ISO]
asas verify-audit --db out/asas.db

asas doctor [--db <path>]                      production readiness checks
asas audit keygen --out <dir>                  Ed25519 key pair for audit anchors
asas audit anchor --db <path>                  sign the chain head into audit.anchor_dir
asas audit verify --db <path> [--anchors <dir>]
asas ops show|set --db <path> ...              audited safe mode

asas llm-eval [--model qwen2.5:7b-instruct] [--base-url URL] [--per-scenario 1]
asas bench [--scales 1,3,10]

asas data profile        --data <dir> [--json]
asas data draft-mapping  --data <dir> --out config/mappings/<name>.toml
asas data check          --data <dir> --mapping <file> [--json]
asas data capabilities   --data <dir> --mapping <file> [--json]
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from asas.core.config import default_config_path, load_config
from asas.core.errors import AsasError
from asas.core.logging import configure_logging
from asas.core.readiness import checks as readiness_checks
from asas.core.security import SYSTEM, Principal, Role
from asas.data.ingest import (
    SourceBundle,
    identity_mapping,
    load_mapping,
    load_source_bundle,
    process_source,
)
from asas.data.profiling import draft_mapping, load_synonyms, profile_directory
from asas.data.synthetic import GeneratorSpec, generate, write_csv, write_vendor_extract
from asas.engine.rules import load_ruleset
from asas.services.demo import ADMIN, ANALYST, ruleset_path, run_demo
from asas.services.evaluation import render_markdown
from asas.services.integration import capability_matrix, render_matrix
from asas.services.platform import Platform, build_gateway
from asas.services.llm_eval import DEFAULT_SCENARIOS as DEFAULT_EVAL_SCENARIOS
from asas.store.anchor import dump as dump_anchor
from asas.store.anchor import generate_ed25519_keypair
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


def _operator() -> Principal:
    """The CLI is a local, privileged operator interface: whoever can run it against the store
    already controls the host. Actions are attributed to the OS account in the audit log."""
    return Principal(user_id=f"cli:{getpass.getuser()}", roles=frozenset({Role.ADMIN}))


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
    if args.style == "vendor":
        write_vendor_extract(dataset, args.out)
        print(f"wrote vendor-style extract (csv, parquet, jsonl, xlsx) to {args.out}")
    else:
        write_csv(dataset, args.out)
        print(f"wrote synthetic SCP/CAL files in contract columns to {args.out}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    platform = _platform(args)
    mapping = load_mapping(args.mapping) if args.mapping else identity_mapping()
    counts = platform.ingest(load_source_bundle(args.data, mapping), ADMIN)
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


def cmd_ops(args: argparse.Namespace) -> int:
    platform = _platform(args)
    if args.ops_command == "set":
        platform.ops.set(
            _operator(),
            bulk_suspended=args.bulk == "suspend",
            llm_suspended=args.llm == "suspend",
            reason=args.reason,
        )
    print(platform.ops.state().model_dump_json(indent=1))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ok, entries = Store(args.db).verify_audit_chain()
    print(f"audit chain valid={ok} entries={entries}")
    return 0 if ok else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    """Run as the schema owner. The application role then starts with auto_migrate = false."""
    store = Store(args.db, auto_migrate=True)
    print(f"applied migrations: {store.applied_migrations or 'none (schema current)'}")
    store.close()
    return 0


def cmd_llm_eval(args: argparse.Namespace) -> int:
    from asas.agents.gateway import OpenAICompatibleGateway
    from asas.services import llm_eval

    cfg = load_config(args.config)
    cfg = cfg.with_value(args.max_prompt_chars, "agents", "max_prompt_chars").with_value(
        args.max_model_calls, "agents", "budgets", "investigator", "max_model_calls"
    )
    cfg = cfg.with_value(args.timeout, "agents", "timeout_seconds").with_value(
        args.timeout * args.max_model_calls, "agents", "budgets", "investigator", "max_seconds"
    )
    gateway = OpenAICompatibleGateway(
        base_url=args.base_url or cfg.string("agents", "base_url"),
        model_id=args.model or cfg.string("agents", "model_id"),
        api_key_env=args.api_key_env or None,
        timeout_seconds=float(args.timeout),
    )
    dataset = generate(GeneratorSpec(days=args.days, seed=args.seed))
    report = llm_eval.run_llm_eval(
        args.workdir,
        cfg,
        dataset,
        gateway,
        scenarios=tuple(s for s in args.scenarios.split(",") if s),
        per_scenario=args.per_scenario,
    )
    if args.json:
        llm_eval.dump(report, args.json)
    markdown = llm_eval.render_markdown(report, args.note)
    if args.report:
        Path(args.report).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from asas.services import bench

    cfg = load_config(args.config)
    report = bench.run_bench(
        args.workdir, cfg, [int(x) for x in args.scales.split(",") if x], days=args.days
    )
    markdown = bench.render_markdown(report)
    if args.report:
        Path(args.report).write_text(markdown, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(markdown)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from asas.domain.models import BulkScope, RuleSpec

    platform = _platform(args)
    rule = RuleSpec.model_validate_json(Path(args.rule_file).read_text("utf-8")) if args.rule_file else None
    scope = None
    if args.bulk_scope:
        hypothesis, _, desk = args.bulk_scope.partition("@")
        scope = BulkScope(hypothesis_type=hypothesis, desk=desk or "*")
    operator = Principal(user_id=f"cli:{getpass.getuser()}", roles=frozenset({Role.INVESTIGATOR}))
    candidate = platform.propose(operator, args.rationale, rule, scope)
    print(f"{candidate.candidate_id} DRAFT; next: replay, counterexamples, shadow, submit, approve")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    results = readiness_checks(cfg, args.db or "")
    for c in results:
        print(f"{c.level:<5} {c.name:<16} {c.detail}")
    env = cfg.string("security", "environment")
    failed = [c for c in results if c.level == "FAIL"]
    print(f"environment={env}: {'NOT READY' if failed else 'ready'}")
    return 1 if failed else 0


def cmd_audit(args: argparse.Namespace) -> int:
    if args.audit_command == "keygen":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        private, public = generate_ed25519_keypair()
        key_path, pub_path = out / "audit-signing-key.pem", out / "audit-public-key.pem"
        if key_path.exists() or pub_path.exists():
            print(f"refusing to overwrite keys in {out}", file=sys.stderr)
            return 2
        key_path.write_bytes(private)
        pub_path.write_bytes(public)
        print(f"wrote {key_path} (keep secret; set ASAS_AUDIT_SIGNING_KEY to its path)")
        print(f"wrote {pub_path} (give to auditors; ASAS_AUDIT_PUBLIC_KEY)")
        return 0
    platform = _platform(args)
    if args.audit_command == "anchor":
        anchor = platform.anchor()
        print(dump_anchor(anchor) if anchor else "audit log is empty: nothing to anchor")
        return 0
    report = platform.verify_audit(args.anchors)
    print(json.dumps(report.to_dict(), indent=1))
    return 0 if report.ok else 1


def cmd_data_profile(args: argparse.Namespace) -> int:
    profiles = profile_directory(args.data)
    if args.json:
        print(json.dumps([p.to_dict() for p in profiles], indent=1, default=str))
        return 0
    for p in profiles:
        print(f"{p.path}: {p.rows} rows {p.error}")
        for name, col in p.columns.items():
            print(
                f"  {name:<32} filled {col.filled}/{p.rows}  distinct {len(col.distinct)}  "
                f"{','.join(sorted(col.kinds))}  e.g. {col.samples[:3]}"
            )
    return 0


def cmd_data_draft(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"refusing to overwrite {out}; pass --force", file=sys.stderr)
        return 2
    synonyms = load_synonyms(args.synonyms) if args.synonyms else load_synonyms()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft_mapping(args.data, synonyms), encoding="utf-8")
    print(f"wrote draft mapping {out}; review the # REVIEW and # TODO lines, then run:")
    print(f"  python -m asas data check --data {args.data} --mapping {out}")
    return 0


def _matrix(args: argparse.Namespace, bundle: SourceBundle) -> dict[str, Any]:
    cfg = load_config(args.config)
    return capability_matrix(bundle, cfg, load_ruleset(ruleset_path(), cfg).rules)


def cmd_data_check(args: argparse.Namespace) -> int:
    mapping = load_mapping(args.mapping)
    report, bundle = process_source(args.data, mapping, args.max_issues)
    matrix = _matrix(args, bundle) if report.ok else None
    if args.json:
        print(json.dumps({"validation": report.to_dict(), "capabilities": matrix}, indent=1))
    else:
        print(report.render())
        if matrix is not None:
            print(render_matrix(matrix))
    return 0 if report.ok else 1


def cmd_data_capabilities(args: argparse.Namespace) -> int:
    bundle = load_source_bundle(args.data, load_mapping(args.mapping))
    matrix = _matrix(args, bundle)
    print(json.dumps(matrix, indent=1) if args.json else render_matrix(matrix))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asas", description="Auditable agentic surveillance")
    parser.add_argument("--config", default=str(default_config_path()))
    sub = parser.add_subparsers(dest="command", required=True)

    def with_db(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        env = os.environ.get("ASAS_DATABASE_URL")
        p.add_argument(
            "--db",
            default=env,
            required=env is None,
            help="SQLite path or postgresql:// URL (default: $ASAS_DATABASE_URL)",
        )
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

    p = sub.add_parser("generate", help="write synthetic SCP/CAL files")
    p.add_argument("--out", required=True)
    p.add_argument(
        "--style",
        choices=("contract", "vendor"),
        default="contract",
        help="contract column names, or a vendor-style extract to practise integration",
    )
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

    p = with_db(sub.add_parser("migrate", help="apply schema migrations (owner role)"))
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("llm-eval", help="evaluate a real model inside the agent runtime")
    p.add_argument("--model")
    p.add_argument("--base-url")
    p.add_argument("--api-key-env", default="")
    p.add_argument("--workdir", default="out/llm-eval")
    p.add_argument("--scenarios", default=",".join(DEFAULT_EVAL_SCENARIOS))
    p.add_argument("--per-scenario", type=int, default=1)
    p.add_argument("--timeout", type=int, default=300, help="seconds per model call")
    p.add_argument("--max-model-calls", type=int, default=20)
    p.add_argument("--max-prompt-chars", type=int, default=12000)
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--report", help="write the markdown report here")
    p.add_argument("--json", help="write the full JSON report here")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_llm_eval)

    p = with_db(sub.add_parser("propose", help="propose a rule or bulk scope (governed)"))
    p.add_argument("--rule-file", help="JSON RuleSpec (same rule_id replaces the rule)")
    p.add_argument("--bulk-scope", help="HYPOTHESIS@DESK, e.g. PRICE_CORRECTION@FX")
    p.add_argument("--rationale", required=True)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("bench", help="stage timings at growing data volumes")
    p.add_argument("--scales", default="1,3,10")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--workdir", default="out/bench")
    p.add_argument("--report")
    p.add_argument("--json")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("doctor", help="production readiness checks (exit 1 on FAIL)")
    p.add_argument("--db", default="", help="database path or URL to check")
    p.set_defaults(func=cmd_doctor)

    audit = sub.add_parser("audit", help="audit anchoring: keygen, anchor, verify")
    asub = audit.add_subparsers(dest="audit_command", required=True)
    p = asub.add_parser("keygen", help="generate an Ed25519 key pair for anchors")
    p.add_argument("--out", required=True)
    p = with_db(asub.add_parser("anchor", help="sign the chain head into audit.anchor_dir"))
    p = with_db(asub.add_parser("verify", help="verify the chain and every external anchor"))
    p.add_argument("--anchors", help="anchor directory (default: audit.anchor_dir)")
    audit.set_defaults(func=cmd_audit)

    ops = with_db(sub.add_parser("ops", help="show or set the audited safe mode"))
    osub = ops.add_subparsers(dest="ops_command", required=True)
    osub.add_parser("show", help="current safe-mode state")
    p = osub.add_parser("set", help="suspend or resume bulk proposals and/or the LLM")
    p.add_argument("--bulk", choices=("suspend", "resume"), required=True)
    p.add_argument("--llm", choices=("suspend", "resume"), required=True)
    p.add_argument("--reason", required=True)
    ops.set_defaults(func=cmd_ops)

    data = sub.add_parser("data", help="integrate real extracts: profile, draft, check")
    dsub = data.add_subparsers(dest="data_command", required=True)
    p = dsub.add_parser("profile", help="describe every extract file in a directory")
    p.add_argument("--data", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_data_profile)
    p = dsub.add_parser("draft-mapping", help="propose a mapping TOML from the headers")
    p.add_argument("--data", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--synonyms")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_data_draft)
    p = dsub.add_parser("check", help="validate every row; list each problem with a fix")
    p.add_argument("--data", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-issues", type=int, default=50)
    p.set_defaults(func=cmd_data_check)
    p = dsub.add_parser("capabilities", help="what works with the fields you have")
    p.add_argument("--data", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_data_capabilities)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AsasError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
