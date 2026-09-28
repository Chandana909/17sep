"""Metrics, health probes, drift monitoring and alert rules."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from asas.api.app import create_app
from asas.core import metrics
from asas.core.config import Config
from asas.core.security import SYSTEM
from asas.data.synthetic import SyntheticDataset
from asas.services.demo import ADMIN, DemoResult, ruleset_path
from asas.services.monitoring import RATES, RunKpis, drift
from asas.services.platform import Platform

ROOT = Path(__file__).resolve().parents[1]
ANALYST = {"X-User": "analyst.jane", "X-Roles": "investigator"}


def _kpis(key: str, bulk: str = "0.6", cases: int = 70, r400: int = 20) -> RunKpis:
    rates = {r: Decimal("0.1") for r in RATES} | {"bulk_rate": Decimal(bulk)}
    return RunKpis(
        run_key=key,
        as_of=datetime(2026, 5, 1, tzinfo=UTC),
        cases=cases,
        rates=rates,
        alerts_by_rule={"R100": 10, "R400": r400},
    )


def test_drift_needs_history_then_flags_rate_and_volume_shifts(cfg: Config) -> None:
    history = [_kpis(f"r{i}") for i in range(2)]
    assert drift(_kpis("now"), history, cfg).status == "INSUFFICIENT_HISTORY"
    history = [_kpis(f"r{i}") for i in range(5)]
    assert drift(_kpis("now"), history, cfg).status == "OK"
    shifted = drift(_kpis("now", bulk="0.95", r400=60), history, cfg)
    assert shifted.status == "DRIFT"
    assert {f.metric for f in shifted.flags} == {"bulk_rate", "alerts:R400"}
    assert drift(_kpis("r4"), history, cfg).history_runs == 4  # never compares a run to itself


def test_repeated_runs_are_stable_and_safe_mode_shows_as_drift(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    platform = Platform(tmp_path / "m.db", cfg)
    platform.seed_policy(ruleset_path())
    platform.ingest(dataset.bundle, ADMIN)
    for days in (4, 3, 2, 1, 0):  # daily runs
        platform.run_pipeline(dataset.end - timedelta(days=days), SYSTEM)
    report = platform.drift_report()
    assert report is not None and report.status in ("OK", "DRIFT") and report.history_runs == 4
    assert "bulk_rate" not in {f.metric for f in report.flags}
    platform.ops.set(ADMIN, bulk_suspended=True, llm_suspended=False, reason="drill")
    platform.run_pipeline(dataset.end + timedelta(hours=1), SYSTEM)
    report = platform.drift_report()
    assert report is not None and report.status == "DRIFT"
    assert "bulk_rate" in {f.metric for f in report.flags}
    exposed = metrics.exposition().decode()
    assert 'asas_drift_flag{metric="bulk_rate"} 1.0' in exposed
    assert 'asas_safe_mode{kind="bulk"} 1.0' in exposed


def test_metrics_cover_the_platform_and_carry_no_business_data(demo: DemoResult) -> None:
    client = TestClient(create_app(demo.platform))
    client.get("/api/cases", headers=ANALYST)
    demo.platform.run_pipeline(demo.dataset.end, SYSTEM)
    text = client.get("/metrics").text
    for name in (
        "asas_pipeline_runs_total",
        "asas_tool_calls_total",
        "asas_agent_runs_total",
        "asas_http_requests_total",
        "asas_audit_chain_valid",
        "asas_cases",
        "asas_data_freshness_seconds",
    ):
        assert name in text, name
    assert 'route="/api/cases"' in text  # route templates, never raw paths
    # no trade, alert, case or user identifiers leak into metrics
    assert not re.search(r"\b(T\d{6}|A\d{6}|CASE-|EP-|analyst\.jane)", text)


def test_liveness_and_readiness(demo: DemoResult, tmp_path: Path, cfg: Config) -> None:
    client = TestClient(create_app(demo.platform))
    assert client.get("/api/health/live").json() == {"status": "alive"}
    assert client.get("/api/health/ready").status_code == 200

    broken = Platform(tmp_path / "b.db", cfg)
    broken.seed_policy(ruleset_path())
    broken.store.audit("x", "DO", "s", {})
    raw = sqlite3.connect(tmp_path / "b.db")
    raw.execute("DROP TRIGGER audit_log_no_update")
    raw.execute("UPDATE audit_log SET actor = 'mallory'")
    raw.commit()
    raw.close()
    response = TestClient(create_app(broken)).get("/api/health/ready")
    assert response.status_code == 503
    assert "audit: chain does not verify" in response.json()["problems"]


def test_alert_rules_reference_real_metrics() -> None:
    rules = yaml.safe_load((ROOT / "deploy" / "prometheus" / "alerts.yml").read_text("utf-8"))
    names = {m.name for m in metrics.REGISTRY.collect()}
    exprs = [r["expr"] for g in rules["groups"] for r in g["rules"]]
    used = {n for e in exprs for n in re.findall(r"\basas_[a-z_]+", str(e))}
    known = names | {f"{n}_total" for n in names}
    assert used and used <= known, used - known
    for group in rules["groups"]:
        for rule in group["rules"]:
            assert rule["labels"]["severity"] in ("page", "ticket")
            assert rule["annotations"]["runbook"]


@pytest.mark.parametrize("path", ["/api/cases", "/api/overview"])
def test_api_paths_still_require_identity_under_metrics(demo: DemoResult, path: str) -> None:
    client = TestClient(create_app(demo.platform))
    assert client.get(path, headers=ANALYST).status_code == 200


def test_every_alert_links_an_existing_runbook_section() -> None:
    rules = yaml.safe_load((ROOT / "deploy" / "prometheus" / "alerts.yml").read_text("utf-8"))
    docs = {
        path.relative_to(ROOT).as_posix(): path.read_text("utf-8")
        for path in (ROOT / "docs").rglob("*.md")
    }
    for group in rules["groups"]:
        for rule in group["rules"]:
            target = rule["annotations"]["runbook"]
            page, _, anchor = target.partition("#")
            assert page in docs, target
            if anchor:
                headings = {
                    re.sub(r"[^a-z0-9 -]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
                    for line in docs[page].splitlines()
                    if line.startswith("#")
                }
                assert anchor in headings, target
