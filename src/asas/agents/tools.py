"""Closed registry of typed, read-only tools (rail: no generic SQL/HTTP/file tools).

Every call passes, in order: capability check -> argument schema validation -> entitlement
check -> cache -> execution with timing -> bounded, typed output -> audit record. Tools read
the frozen snapshot; none can write, and none returns raw row sets.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from asas.core.errors import PermissionDenied, ToolError, ToolNotAllowed
from asas.core.ids import content_hash, stable_id
from asas.core.security import Principal
from asas.core.tracing import NOOP_TRACER, Tracer
from asas.domain.models import RuleSpec
from asas.engine.evidence import facts
from asas.engine.hypotheses import EVIDENCE_FUNCTIONS
from asas.engine.snapshot import Snapshot


class ToolOutputList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[dict[str, str], ...]
    lead_only: bool = False
    note: str = ""


class ToolContext:
    """What a tool may see: the snapshot, the principal, and read-only services."""

    def __init__(
        self,
        snapshot: Snapshot,
        principal: Principal,
        graph_neighborhood: Callable[[str, int], BaseModel] | None = None,
        similar_cases: Callable[[str, int], ToolOutputList] | None = None,
        replay_rule: Callable[[str], BaseModel] | None = None,
        simulate_rule: Callable[[RuleSpec], BaseModel] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.principal = principal
        self.graph_neighborhood = graph_neighborhood
        self.similar_cases = similar_cases
        self.replay_rule = replay_rule
        self.simulate_rule = simulate_rule

    def check_episode(self, episode_id: str) -> None:
        ep = self.snapshot.episodes_by_id.get(episode_id)
        if ep is None:
            raise ToolError(f"unknown episode {episode_id}")
        if not any(self.principal.sees_desk(d) for d in ep.desks):
            raise PermissionDenied(f"{self.principal.user_id} is not entitled to {episode_id}")

    def check_trade(self, trade_id: str) -> None:
        events = self.snapshot.events_by_trade.get(trade_id)
        if events and not self.principal.sees_desk(events[0].desk):
            raise PermissionDenied(f"{self.principal.user_id} is not entitled to {trade_id}")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: tuple[str, ...]
    run: Callable[[ToolContext, Mapping[str, str]], BaseModel]
    lead_only: bool = False


def _evidence_tool(name: str, description: str, *args: str) -> ToolSpec:
    fn = EVIDENCE_FUNCTIONS[name]

    def run(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
        if "episode_id" in a:
            ctx.check_episode(a["episode_id"])
        for key in ("trade_id", "trade_a", "trade_b"):
            if key in a:
                ctx.check_trade(a[key])
        return fn(ctx.snapshot, a, ctx.principal)

    return ToolSpec(name, description, args, run)


def _subrules(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    rules = [r for r in ctx.snapshot.policy.ruleset.rules if r.rule_id == a["rule_id"]]
    return ToolOutputList(
        items=tuple(
            {
                "subrule_id": r.subrule_id,
                "severity": r.severity,
                "description": r.description,
                "conditions": str(len(r.conditions)),
            }
            for r in rules
        )
    )


def _parameters(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    rules = [r for r in ctx.snapshot.policy.ruleset.rules if r.rule_id == a["rule_id"]]
    return ToolOutputList(
        items=tuple(
            {"subrule_id": r.subrule_id, "parameter": k, "value": v}
            for r in rules
            for k, v in sorted(r.parameters.items())
        )
    )


def _graph(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    if ctx.graph_neighborhood is None:
        raise ToolError("evidence graph not available")
    depth = int(a.get("depth", "1") or "1")
    return ctx.graph_neighborhood(a["node_id"], depth)


def _similar(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    if ctx.similar_cases is None:
        raise ToolError("memory index not available")
    ctx.check_episode(a["episode_id"])
    return ctx.similar_cases(a["episode_id"], int(a.get("k", "5") or "5"))


def _replay(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    if ctx.replay_rule is None:
        raise ToolError("replay not available")
    return ctx.replay_rule(a["rule_id"])


def _simulate(ctx: ToolContext, a: Mapping[str, str]) -> BaseModel:
    if ctx.simulate_rule is None:
        raise ToolError("simulation not available")
    try:
        rule = RuleSpec.model_validate(json.loads(a["rule_json"]))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ToolError(f"rule_json is not a valid rule: {exc}") from exc
    return ctx.simulate_rule(rule)


TOOLS: Mapping[str, ToolSpec] = {
    t.name: t
    for t in (
        _evidence_tool(
            "get_alert", "one SCP alert with its explanation delimited as untrusted", "alert_id"
        ),
        _evidence_tool(
            "get_episode", "episode members, links, quality flags, signals, score", "episode_id"
        ),
        _evidence_tool("get_trade", "latest version of a trade", "trade_id"),
        _evidence_tool("get_trade_history", "every version of a trade", "trade_id"),
        _evidence_tool(
            "compare_trade_versions", "field-level differences between versions", "trade_id"
        ),
        _evidence_tool(
            "get_event_sequence", "ordered lifecycle events across the episode", "episode_id"
        ),
        _evidence_tool(
            "get_related_alerts",
            "alerts in the episode and nearby on the same book or instrument",
            "episode_id",
        ),
        _evidence_tool(
            "get_related_episodes", "nearby episodes and unresolved link candidates", "episode_id"
        ),
        _evidence_tool(
            "get_trader_baseline",
            "trader amend/cancel rates vs desk (person-data entitlement; raise-only)",
            "episode_id",
        ),
        _evidence_tool(
            "get_peer_comparison",
            "percentile and robust z of a signal vs frozen peers",
            "episode_id",
            "signal",
        ),
        _evidence_tool(
            "get_recurrence", "how often this pattern recurred in the book/instrument", "episode_id"
        ),
        _evidence_tool(
            "get_prior_outcomes",
            "curated outcomes of prior episodes with this pattern",
            "episode_id",
        ),
        _evidence_tool("get_rule", "production rule definition", "rule_id"),
        _evidence_tool(
            "compare_trades",
            "economic comparison of a cancelled trade and a candidate rebook",
            "trade_a",
            "trade_b",
        ),
        ToolSpec("get_subrules", "subrules of a production rule", ("rule_id",), _subrules),
        ToolSpec(
            "get_rule_parameters", "parameters of a production rule", ("rule_id",), _parameters
        ),
        ToolSpec(
            "search_evidence_graph",
            "bounded neighbourhood in the evidence graph",
            ("node_id", "depth"),
            _graph,
        ),
        ToolSpec(
            "find_similar_cases",
            "similar past episodes (a lead, never evidence)",
            ("episode_id", "k"),
            _similar,
            lead_only=True,
        ),
        ToolSpec(
            "replay_rule", "historical replay metrics of an existing rule", ("rule_id",), _replay
        ),
        ToolSpec(
            "simulate_candidate_rule",
            "historical metrics of a candidate rule (no side effects)",
            ("rule_json",),
            _simulate,
        ),
    )
}
TOOL_SCHEMA_VERSION = content_hash([(t.name, t.args, t.lead_only) for t in TOOLS.values()])[:16]


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    call_id: str
    tool: str
    args: dict[str, str]
    ok: bool
    output: dict[str, Any] | None
    output_type: str | None
    facts: dict[str, str]
    error: str
    cached: bool
    lead_only: bool
    duration_ms: int


@dataclass
class ToolExecutor:
    ctx: ToolContext
    allowed: frozenset[str]
    run_id: str
    recorder: Callable[[ToolResult, int], None] | None = None
    tracer: Tracer = NOOP_TRACER
    cache: dict[str, tuple[BaseModel, str]] = field(default_factory=dict)
    calls: int = 0

    def call(self, tool: str, args: Mapping[str, str], step: int) -> ToolResult:
        clean = {str(k): str(v) for k, v in args.items()}
        call_id = stable_id("CALL", self.run_id, str(step), tool, json.dumps(clean, sort_keys=True))
        started = time.perf_counter()
        spec = TOOLS.get(tool)
        with self.tracer.span("tool", tool=tool, run_id=self.run_id) as span:
            try:
                if spec is None or tool not in self.allowed:
                    raise ToolNotAllowed(f"tool {tool!r} is not in this agent's capability set")
                missing = [a for a in spec.args if a not in clean and a not in ("depth", "k")]
                extra = [a for a in clean if a not in spec.args]
                if missing or extra:
                    raise ToolError(f"bad arguments: missing={missing} unexpected={extra}")
                key = content_hash(
                    [
                        tool,
                        clean,
                        self.ctx.snapshot.snapshot_id,
                        sorted(self.ctx.principal.desks),
                        self.ctx.principal.person_data,
                    ]
                )
                cached = key in self.cache
                if cached:
                    output = self.cache[key][0]
                else:
                    output = spec.run(self.ctx, clean)
                    self.cache[key] = (output, type(output).__name__)
                self.calls += 1
                result = ToolResult(
                    call_id=call_id,
                    tool=tool,
                    args=clean,
                    ok=True,
                    output=output.model_dump(mode="json"),
                    output_type=type(output).__name__,
                    facts=facts(output),
                    error="",
                    cached=cached,
                    lead_only=spec.lead_only,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            except (ToolError, PermissionDenied, KeyError, ValueError) as exc:
                span.set("error", type(exc).__name__)
                result = ToolResult(
                    call_id=call_id,
                    tool=tool,
                    args=clean,
                    ok=False,
                    output=None,
                    output_type=None,
                    facts={},
                    error=f"{type(exc).__name__}: {exc}",
                    cached=False,
                    lead_only=bool(spec and spec.lead_only),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
        if self.recorder is not None:
            self.recorder(result, step)
        return result


def tool_catalog(allowed: frozenset[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "args": list(t.args),
            "description": t.description,
            "lead_only": t.lead_only,
        }
        for t in TOOLS.values()
        if t.name in allowed
    ]
