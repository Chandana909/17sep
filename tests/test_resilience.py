"""Fallbacks and operational controls: model endpoint chain, safe mode, data gates,
concurrent pipeline, and failure attribution."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from asas.agents.gateway import (
    CachingGateway,
    FallbackGateway,
    ModelRequest,
    ScriptedGateway,
)
from asas.core.config import Config
from asas.core.errors import GovernanceError, ModelError, PermissionDenied
from asas.core.security import SYSTEM
from asas.data.ingest import SourceBundle
from asas.data.synthetic import SyntheticDataset
from asas.domain.models import Recommendation
from asas.engine.gates import data_gates
from asas.services.demo import ADMIN, ANALYST, ruleset_path
from asas.services.platform import Platform, build_gateway

REQUEST = ModelRequest(
    model_id="primary", prompt_id="p", prompt_version="1", system="s", user="u", max_tokens=10
)


def _down(_: ModelRequest) -> str:
    raise ModelError("connection refused")


def _platform(path: Path, cfg: Config, bundle: SourceBundle) -> Platform:
    platform = Platform(path, cfg)
    platform.seed_policy(ruleset_path())
    platform.ingest(bundle, ADMIN)
    return platform


# ---------------------------------------------------------------- model endpoints


def test_fallback_chain_uses_the_next_endpoint_and_records_who_answered() -> None:
    primary = ScriptedGateway(_down, model_id="qwen-local")
    secondary = ScriptedGateway(lambda r: f'{{"model": "{r.model_id}"}}', model_id="qwen-backup")
    chain = FallbackGateway([primary, secondary])
    response = chain.complete(REQUEST)
    assert response.model_id == "qwen-backup"
    assert secondary.calls[0].model_id == "qwen-backup"  # re-issued with its own model id
    with pytest.raises(ModelError, match="all model endpoints failed"):
        FallbackGateway([primary, ScriptedGateway(_down, model_id="b")]).complete(REQUEST)


def test_configured_fallbacks_build_one_cached_chain(cfg: Config, tmp_path: Path) -> None:
    enabled = cfg.with_value(True, "agents", "enabled").with_value(
        [{"base_url": "http://backup:8000/v1", "model_id": "qwen-backup"}], "agents", "fallbacks"
    )
    platform = Platform(tmp_path / "g.db", enabled)
    gateway = build_gateway(enabled, platform.store)
    assert isinstance(gateway, CachingGateway)
    assert gateway.model_id == enabled.string("agents", "model_id")
    assert build_gateway(cfg, platform.store) is None  # agents disabled: playbook only


# ---------------------------------------------------------------- safe mode


@pytest.fixture(scope="module")
def platform(
    tmp_path_factory: pytest.TempPathFactory, cfg: Config, dataset: SyntheticDataset
) -> Platform:
    return _platform(tmp_path_factory.mktemp("res") / "r.db", cfg, dataset.bundle)


def test_safe_mode_needs_an_admin_and_a_reason(platform: Platform) -> None:
    with pytest.raises(PermissionDenied):
        platform.ops.set(ANALYST, bulk_suspended=True, llm_suspended=False, reason="x")
    with pytest.raises(GovernanceError, match="reason"):
        platform.ops.set(ADMIN, bulk_suspended=True, llm_suspended=False, reason="  ")


def test_safe_mode_blocks_bulk_and_the_llm_and_is_audited(
    platform: Platform, dataset: SyntheticDataset
) -> None:
    normal = platform.run_pipeline(dataset.end, SYSTEM)
    assert normal.proposed_bulk > 0 and not normal.degraded
    platform.ops.set(ADMIN, bulk_suspended=True, llm_suspended=True, reason="model incident 42")
    try:
        suspended = platform.run_pipeline(dataset.end, SYSTEM)
        assert suspended.safe_mode and suspended.llm_suspended and suspended.degraded
        assert suspended.proposed_bulk == 0
        assert suspended.escalation == normal.escalation  # escalations are never suppressed
        assert not platform.runtime.llm_enabled
        for case in platform.cases():
            assert case.recommendation is not Recommendation.PROPOSED_BULK
        reasons = {r for c in platform.cases() for r in c.reasons}
        assert "SAFE_MODE:model incident 42" in reasons
        actions = [e["action"] for e in platform.store.audit_entries(50)]
        assert "OPS_STATE" in actions
    finally:
        platform.ops.set(ADMIN, bulk_suspended=False, llm_suspended=False, reason="resolved")
    resumed = platform.run_pipeline(dataset.end, SYSTEM)
    assert resumed.proposed_bulk == normal.proposed_bulk and not resumed.degraded
    latest = platform.latest_run()  # regression: re-running an earlier run must re-point
    assert latest is not None and latest.run_key == resumed.run_key and not latest.safe_mode
    assert [s.reason for s in platform.ops.history()][-2:] == ["model incident 42", "resolved"]


# ---------------------------------------------------------------- data gates


def test_gates_pass_on_complete_fresh_data(cfg: Config, dataset: SyntheticDataset) -> None:
    assert all(g.passed for g in data_gates(dataset.bundle, dataset.end, cfg))


def test_stale_feed_blocks_bulk(cfg: Config, dataset: SyntheticDataset, tmp_path: Path) -> None:
    platform = _platform(tmp_path / "stale.db", cfg, dataset.bundle)
    later = dataset.end + timedelta(days=10)  # nothing arrived for ten days
    report = platform.run_pipeline(later, SYSTEM)
    assert report.degraded and report.proposed_bulk == 0
    assert any(g.startswith("STALE_TRADE_EVENTS") for g in report.data_gates)


def test_truncated_load_blocks_bulk(cfg: Config, dataset: SyntheticDataset) -> None:
    cut = dataset.end - timedelta(days=7)  # only one record in five of the last week arrived
    events = dataset.bundle.trade_events
    partial = SourceBundle(
        trade_events=tuple(e for i, e in enumerate(events) if e.record_time < cut or i % 5 == 0),
        alerts=dataset.bundle.alerts,
    )
    names = {g.name for g in data_gates(partial, dataset.end, cfg) if not g.passed}
    assert "LOW_VOLUME_TRADE_EVENTS" in names


# ---------------------------------------------------------------- concurrency and failures


def test_parallel_investigations_decide_exactly_like_sequential(
    cfg: Config, dataset: SyntheticDataset, tmp_path: Path, platform: Platform
) -> None:
    parallel_cfg = cfg.with_value(4, "pipeline", "max_workers")
    parallel = _platform(tmp_path / "par.db", parallel_cfg, dataset.bundle)
    report = parallel.run_pipeline(dataset.end, SYSTEM)
    assert report.workers == 4 and report.agent_failures == 0
    platform.run_pipeline(dataset.end, SYSTEM)

    def decisions(p: Platform) -> dict[str, tuple[str, str | None]]:
        return {c.case_id: (c.recommendation.value, c.conclusion) for c in p.cases()}

    assert decisions(parallel) == decisions(platform)
    assert parallel.store.verify_audit_chain()[0]


def test_a_failed_run_is_audited(
    cfg: Config, dataset: SyntheticDataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _platform(tmp_path / "broken.db", cfg, dataset.bundle)

    def explode(_as_of: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(broken, "snapshot", explode)
    with pytest.raises(RuntimeError):
        broken.run_pipeline(dataset.end, SYSTEM)
    last = broken.store.audit_entries(1)[0]
    assert last["action"] == "PIPELINE_FAILED" and last["detail"] == {"error": "RuntimeError"}
