"""Agent runtime and agent programs: dynamic investigation, injection resistance, fallback,
checkpoint/resume, idempotency, budgets, closed tools, lead-only memory, entitlements."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from analysts import adaptive_analyst, garbage_model, injected_analyst
from asas.agents import tools as tool_module
from asas.agents.gateway import (
    CircuitBreaker,
    ModelRequest,
    ResilientGateway,
    ScriptedGateway,
)
from asas.agents.investigator import InvestigatorProgram
from asas.agents.tools import ToolExecutor
from asas.agents.validation import clean_model_text, validate_prose
from asas.core.config import Config
from asas.core.errors import ModelError
from asas.core.security import SYSTEM, Principal, Role
from asas.data.synthetic import INJECTION_TEXT, SyntheticDataset
from asas.domain.models import HypothesisStatus
from asas.engine.decisions import case_id_for
from asas.services.demo import ADMIN, ruleset_path
from asas.services.platform import Platform, wrap_gateway


def _platform(path: Path, cfg: Config, dataset: SyntheticDataset, gateway=None) -> Platform:  # type: ignore[no-untyped-def]
    platform = Platform(path, cfg, gateway)
    platform.seed_policy(ruleset_path())
    platform.ingest(dataset.bundle, ADMIN)
    return platform


def _episode(
    platform: Platform, dataset: SyntheticDataset, scenario: str, recent: bool = True
) -> str:
    snap = platform.snapshot(dataset.end)
    for e in reversed(snap.episodes):
        matches = dataset.truth.scenario_by_trade.get(e.trade_ids[0]) == scenario and e.alert_ids
        if matches and (not recent or e.start > dataset.end - timedelta(days=30)):
            return e.episode_id
    raise AssertionError(f"no {scenario} episode")


@pytest.fixture(scope="module")
def playbook(
    tmp_path_factory: pytest.TempPathFactory, cfg: Config, dataset: SyntheticDataset
) -> Iterator[Platform]:
    yield _platform(tmp_path_factory.mktemp("pb") / "a.db", cfg, dataset)


def _llm(cfg: Config) -> Config:
    return cfg.with_value(True, "agents", "enabled")


def test_playbook_investigation_is_evidence_grounded(
    playbook: Platform, dataset: SyntheticDataset
) -> None:
    eid = _episode(playbook, dataset, "FAT_FINGER")
    result = playbook.investigate(eid, dataset.end, SYSTEM)
    assert result.conclusion == "PRICE_CORRECTION" and result.policy == "playbook"
    supported = next(h for h in result.hypotheses if h.type == "PRICE_CORRECTION")
    assert supported.status is HypothesisStatus.SUPPORTED and supported.evidence_ids
    competing = {h.type for h in result.hypotheses}
    assert {
        "OFF_MARKET_AMENDMENT",
        "RECURRING_BENIGN_CONTEXT",
    } <= competing  # alternatives considered
    assert all(e.tool in tool_module.TOOLS for e in result.evidence)


def test_idempotent_runs_replay_by_record(playbook: Platform, dataset: SyntheticDataset) -> None:
    eid = _episode(playbook, dataset, "OFF_MARKET")
    first = playbook.investigate(eid, dataset.end, SYSTEM)
    ctx = playbook.tool_context(dataset.end, SYSTEM, "investigator")
    again = playbook.runtime.run(InvestigatorProgram(case_id_for(eid)), eid, ctx)
    assert again.replayed and again.result == first


def test_llm_driven_investigation_takes_its_own_path_to_the_verified_answer(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset, playbook: Platform
) -> None:
    gateway = ScriptedGateway(adaptive_analyst, model_id="scripted-analyst")
    platform = _platform(tmp_path / "llm.db", _llm(cfg), dataset, gateway)
    for scenario, expected in (
        ("FAT_FINGER", "PRICE_CORRECTION"),
        ("OFF_MARKET", "OFF_MARKET_AMENDMENT"),
        ("CANCEL_REBOOK", "CANCEL_REBOOK_CORRECTION"),
    ):
        eid = _episode(platform, dataset, scenario)
        llm = platform.investigate(eid, dataset.end, SYSTEM)
        det = playbook.investigate(eid, dataset.end, SYSTEM)
        assert llm.policy == "llm" and not llm.fallback_used
        assert llm.conclusion == det.conclusion == expected
        assert [e.tool for e in llm.evidence] != [e.tool for e in det.evidence]  # a different route
        assert "get_related_alerts" in {e.tool for e in llm.evidence}
    manifest = platform.run_trace(llm.run_id)["manifest"]
    assert manifest["model_id"] == "scripted-analyst" and manifest["prompt_version"]


def test_prompt_injection_cannot_produce_an_unsupported_conclusion(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    injected = [a for a in dataset.bundle.alerts if a.explanation == INJECTION_TEXT]
    assert injected, "the synthetic data plants injection text"
    gateway = ScriptedGateway(injected_analyst)
    platform = _platform(tmp_path / "inj.db", _llm(cfg), dataset, gateway)
    eid = _episode(platform, dataset, "OFF_MARKET")
    result = platform.investigate(eid, dataset.end, SYSTEM)
    assert result.conclusion != "PRICE_CORRECTION"
    # the model keeps concluding without support; every attempt is rejected, and once its model
    # budget is spent the deterministic playbook finishes the job on the evidence
    assert result.conclusion in (None, "OFF_MARKET_AMENDMENT") and result.fallback_used
    trace = platform.run_trace(result.run_id)
    rejected = [s for s in trace["steps"] if "rejected" in s["observation"]]
    assert rejected, "every unsupported conclusion attempt is rejected and recorded"


def test_garbage_model_output_falls_back_to_playbook(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    platform = _platform(tmp_path / "g.db", _llm(cfg), dataset, ScriptedGateway(garbage_model))
    eid = _episode(platform, dataset, "FAT_FINGER")
    result = platform.investigate(eid, dataset.end, SYSTEM)
    assert result.fallback_used and result.conclusion == "PRICE_CORRECTION"


def test_model_outage_opens_circuit_and_falls_back(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    calls = {"n": 0}

    def down(_: ModelRequest) -> str:
        calls["n"] += 1
        raise ModelError("connection refused")

    store_path = tmp_path / "down.db"
    platform = _platform(store_path, _llm(cfg), dataset)
    platform.runtime.gateway = wrap_gateway(ScriptedGateway(down), _llm(cfg), platform.store)
    eid = _episode(platform, dataset, "CANCEL_REBOOK")
    result = platform.investigate(eid, dataset.end, SYSTEM)
    assert result.fallback_used and result.conclusion == "CANCEL_REBOOK_CORRECTION"
    assert calls["n"] <= cfg.integer("agents", "breaker_threshold") + 1  # breaker stops hammering


def test_resilient_gateway_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def flaky(_: ModelRequest) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ModelError("timeout")
        return "{}"

    gateway = ResilientGateway(
        ScriptedGateway(flaky),
        retries=2,
        backoff_seconds=0,
        breaker=CircuitBreaker(5, 60),
        sleep=lambda _: None,
    )
    request = ModelRequest(
        model_id="m", prompt_id="p", prompt_version="1", system="s", user="u", max_tokens=10
    )
    assert gateway.complete(request).text == "{}" and attempts["n"] == 3


def test_crash_mid_run_resumes_from_checkpoint(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = _platform(tmp_path / "crash.db", cfg, dataset)
    eid = _episode(platform, dataset, "OFF_MARKET")
    original = ToolExecutor.call
    state = {"calls": 0}

    def crashing(self: ToolExecutor, tool: str, args: dict[str, str], step: int):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 3:
            raise RuntimeError("worker killed")
        return original(self, tool, args, step)

    monkeypatch.setattr(ToolExecutor, "call", crashing)
    with pytest.raises(RuntimeError):
        platform.investigate(eid, dataset.end, SYSTEM)
    monkeypatch.setattr(ToolExecutor, "call", original)
    steps_before = len(platform.run_trace(platform.runs_for(eid)[0])["steps"])
    ctx = platform.tool_context(dataset.end, SYSTEM, "investigator")
    outcome = platform.runtime.run(InvestigatorProgram(case_id_for(eid)), eid, ctx)
    assert outcome.resumed and outcome.result.conclusion == "OFF_MARKET_AMENDMENT"
    steps_after = platform.run_trace(outcome.result.run_id)["steps"]
    assert [s["step"] for s in steps_after] == list(range(1, len(steps_after) + 1))
    assert len(steps_after) > steps_before


def test_budget_exhaustion_abstains(tmp_path: Path, cfg: Config, dataset: SyntheticDataset) -> None:
    tight = cfg.with_value(3, "agents", "budgets", "investigator", "max_steps")
    platform = _platform(tmp_path / "b.db", tight, dataset)
    eid = _episode(platform, dataset, "FAT_FINGER")
    result = platform.investigate(eid, dataset.end, SYSTEM)
    assert result.abstained and result.abstain_reason == "BUDGET_STEPS"


def test_tool_registry_is_closed_and_entitled(
    playbook: Platform, dataset: SyntheticDataset
) -> None:
    ctx = playbook.tool_context(dataset.end, SYSTEM, "investigator")
    executor = ToolExecutor(ctx=ctx, allowed=frozenset({"get_episode"}), run_id="R")
    assert "ToolNotAllowed" in executor.call("run_sql", {"q": "drop table"}, 0).error
    assert (
        "ToolNotAllowed"
        in executor.call("compare_trades", {"trade_a": "x", "trade_b": "y"}, 0).error
    )
    eid = _episode(playbook, dataset, "FAT_FINGER")
    assert executor.call("get_episode", {"episode_id": eid}, 0).ok
    fx_only = Principal(user_id="fx", roles=frozenset({Role.INVESTIGATOR}), desks=frozenset({"FX"}))
    eq_episode = _episode(playbook, dataset, "OFF_MARKET")
    scoped = ToolExecutor(
        ctx=playbook.tool_context(dataset.end, fx_only, "investigator"),
        allowed=frozenset({"get_episode", "get_trader_baseline"}),
        run_id="R2",
    )
    assert "PermissionDenied" in scoped.call("get_episode", {"episode_id": eq_episode}, 0).error
    assert "PermissionDenied" in scoped.call("get_trader_baseline", {"episode_id": eid}, 0).error
    assert not {"approve", "submit", "confirm_link", "record_decision"} & set(tool_module.TOOLS)


def test_similarity_is_a_lead_never_evidence(playbook: Platform, dataset: SyntheticDataset) -> None:
    ctx = playbook.tool_context(dataset.end, SYSTEM, "investigator")
    executor = ToolExecutor(ctx=ctx, allowed=frozenset({"find_similar_cases"}), run_id="R3")
    eid = _episode(playbook, dataset, "FAT_FINGER")
    result = executor.call("find_similar_cases", {"episode_id": eid, "k": "3"}, 0)
    assert result.ok and result.lead_only and result.output and result.output["lead_only"]


def test_prose_validation_and_model_text_cleaning() -> None:
    assert validate_prose("price moved 12 percent", {"E1"}) == ["NUMBER_OUTSIDE_CITATION"]
    assert validate_prose("supported by [E1] and [E9]", {"E1"}) == ["UNKNOWN_EVIDENCE:E9"]
    assert validate_prose("supported by [E1]", {"E1"}) == []
    assert (
        clean_model_text('<think>12</think>```json\n{"action": "abstain"}\n```')
        == '{"action": "abstain"}'
    )
