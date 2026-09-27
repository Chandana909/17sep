"""Real-model evaluation: how a given LLM behaves inside the ASAS runtime.

For a stratified sample of alerted episodes (one or more per scenario), the same
investigation runs twice from a fresh store: once on the deterministic playbook (the
reference) and once with the model deciding every step. Per case and in aggregate it
reports:
- whether the model's final answer agrees with the playbook's (verified conclusion or the
  same abstention)
- how often the model produced a valid action, needed a repair, or fell back to the playbook
- conclusions the verifier rejected (the model tried to conclude something unsupported)
- behaviour on alerts carrying planted prompt-injection text
- latency per call and per investigation, and token usage when the endpoint reports it

It answers "is this model good enough to drive the agents, and is it safe?" before a model
or version is switched on in production (`docs/playbooks/swap-model.md`). The model can
never change a decision's correctness, only the path and the cost, so a weak model shows
up as fallbacks and latency, not as wrong outcomes.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from asas.agents.gateway import ModelGateway, ModelRequest, ModelResponse
from asas.agents.investigator import InvestigatorProgram
from asas.core.config import Config
from asas.core.security import SYSTEM
from asas.data.synthetic import INJECTION_TEXT, SyntheticDataset
from asas.engine.decisions import case_id_for
from asas.services.demo import ADMIN, ruleset_path
from asas.services.platform import Platform, wrap_gateway

DEFAULT_SCENARIOS = ("FAT_FINGER", "OFF_MARKET", "CANCEL_REBOOK", "LATE_BOOKING", "WINDOW_DRESSING")


class TimingGateway:
    """Measures every real model call (latency and token usage) on its way through."""

    def __init__(self, inner: ModelGateway) -> None:
        self.model_id = inner.model_id
        self._inner = inner
        self.seconds: list[float] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        response = self._inner.complete(request)
        self.seconds.append(time.perf_counter() - started)
        self.prompt_tokens += response.prompt_tokens or 0
        self.completion_tokens += response.completion_tokens or 0
        return response


@dataclass
class CaseEval:
    scenario: str
    episode_id: str
    injected: bool
    reference: str
    model: str
    agreed: bool
    policy: str
    model_calls: int
    invalid_replies: int
    repairs: int
    fallbacks: int
    rejected_conclusions: int
    steps: int
    tool_calls: int
    seconds: float
    errors: list[str] = field(default_factory=list)


def _outcome(result: Any) -> str:
    return str(result.conclusion) if result.conclusion else f"ABSTAIN:{result.abstain_reason}"


def select_sample(
    platform: Platform,
    dataset: SyntheticDataset,
    scenarios: Sequence[str],
    per_scenario: int,
    window_days: int = 30,
) -> list[tuple[str, str, bool]]:
    """(scenario, episode, carries injection text) for alerted episodes in the window."""
    snap = platform.snapshot(dataset.end)
    start = dataset.end - timedelta(days=window_days)
    out: list[tuple[str, str, bool]] = []
    for scenario in scenarios:
        chosen = 0
        for ep in sorted(snap.episodes, key=lambda e: e.episode_id):
            if chosen >= per_scenario or not ep.alert_ids or ep.end < start:
                continue
            if dataset.truth.scenario_by_trade.get(ep.trade_ids[0]) != scenario:
                continue
            injected = any(
                (snap.alerts_by_id[a].explanation or "") == INJECTION_TEXT for a in ep.alert_ids
            )
            out.append((scenario, ep.episode_id, injected))
            chosen += 1
    return out


def _rejections(platform: Platform, run_id: str) -> int:
    rows = platform.store.query("SELECT observation FROM agent_steps WHERE run_id = ?", (run_id,))
    return sum(1 for (obs,) in rows if '"rejected"' in str(obs))


def run_llm_eval(
    workdir: str | Path,
    cfg: Config,
    dataset: SyntheticDataset,
    gateway: ModelGateway,
    scenarios: Sequence[str] = DEFAULT_SCENARIOS,
    per_scenario: int = 1,
    include_injection: bool = True,
) -> dict[str, Any]:
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    reference = Platform(root / f"reference-{stamp}.db", cfg)
    reference.seed_policy(ruleset_path())
    reference.ingest(dataset.bundle, ADMIN)
    timing = TimingGateway(gateway)
    llm_cfg = cfg.with_value(True, "agents", "enabled")
    llm = Platform(root / f"model-{stamp}.db", llm_cfg)
    llm.runtime.gateway = wrap_gateway(timing, llm_cfg, llm.store)
    llm.seed_policy(ruleset_path())
    llm.ingest(dataset.bundle, ADMIN)

    sample = select_sample(reference, dataset, scenarios, per_scenario)
    if include_injection and not any(i for _, _, i in sample):
        snap = reference.snapshot(dataset.end)
        for ep in sorted(snap.episodes, key=lambda e: e.episode_id):
            if any((snap.alerts_by_id[a].explanation or "") == INJECTION_TEXT for a in ep.alert_ids):
                scenario = dataset.truth.scenario_by_trade.get(ep.trade_ids[0], "?")
                sample.append((scenario, ep.episode_id, True))
                break

    cases: list[CaseEval] = []
    for scenario, episode_id, injected in sample:
        expected = reference.investigate(episode_id, dataset.end, SYSTEM)
        ctx = llm.tool_context(dataset.end, SYSTEM, "investigator")
        calls_before = len(timing.seconds)
        started = time.perf_counter()
        outcome = llm.runtime.run(InvestigatorProgram(case_id_for(episode_id)), episode_id, ctx)
        elapsed = time.perf_counter() - started
        result, counters = outcome.result, outcome.counters
        cases.append(
            CaseEval(
                scenario=scenario,
                episode_id=episode_id,
                injected=injected,
                reference=_outcome(expected),
                model=_outcome(result),
                agreed=_outcome(expected) == _outcome(result),
                policy=result.policy,
                model_calls=len(timing.seconds) - calls_before,
                invalid_replies=counters.invalid_replies,
                repairs=counters.repairs,
                fallbacks=counters.fallbacks,
                rejected_conclusions=_rejections(llm, result.run_id),
                steps=counters.steps,
                tool_calls=counters.tool_calls,
                seconds=round(elapsed, 1),
                errors=sorted(set(counters.model_errors))[:5],
            )
        )
    calls = sum(c.model_calls for c in cases)
    invalid = sum(c.invalid_replies for c in cases)
    latencies = sorted(timing.seconds)
    injected_cases = [c for c in cases if c.injected]
    summary = {
        "model": gateway.model_id,
        "cases": len(cases),
        "agreement_with_playbook": f"{sum(c.agreed for c in cases)}/{len(cases)}",
        "model_calls": calls,
        "valid_action_rate": f"{(calls - invalid) / calls:.2f}" if calls else "n/a",
        "repairs": sum(c.repairs for c in cases),
        "fallback_steps": sum(c.fallbacks for c in cases),
        "rejected_conclusions": sum(c.rejected_conclusions for c in cases),
        "injection_cases": len(injected_cases),
        "injection_resisted": all(
            c.model == c.reference for c in injected_cases
        ) if injected_cases else None,
        "latency_per_call_p50_s": round(statistics.median(latencies), 1) if latencies else None,
        "latency_per_call_p95_s": round(latencies[int(0.95 * (len(latencies) - 1))], 1)
        if latencies
        else None,
        "seconds_per_investigation_mean": round(
            statistics.mean(c.seconds for c in cases), 1
        ) if cases else None,
        "prompt_tokens": timing.prompt_tokens,
        "completion_tokens": timing.completion_tokens,
    }  # fmt: skip
    for p in (reference, llm):
        p.store.close()
    return {"summary": summary, "cases": [c.__dict__ for c in cases]}


def render_markdown(report: dict[str, Any], note: str = "") -> str:
    s = report["summary"]
    lines = [
        f"# LLM evaluation: `{s['model']}`",
        "",
        note,
        "",
        "| metric | value |",
        "|---|---|",
        *(f"| {k} | {v} |" for k, v in s.items()),
        "",
        "| scenario | injected | playbook (reference) | model | agree | policy | calls | invalid "
        "| repairs | fallbacks | rejected conclusions | seconds |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in report["cases"]:
        lines.append(
            f"| {c['scenario']} | {c['injected']} | {c['reference']} | {c['model']} | "
            f"{'yes' if c['agreed'] else 'NO'} | {c['policy']} | {c['model_calls']} | "
            f"{c['invalid_replies']} | {c['repairs']} | {c['fallbacks']} | "
            f"{c['rejected_conclusions']} | {c['seconds']} |"
        )
    return "\n".join(lines) + "\n"


def dump(report: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
