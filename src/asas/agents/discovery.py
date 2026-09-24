"""Pattern / Rule Discovery agent.

Works through mined patterns: tests candidate rules with `simulate_candidate_rule`, may refine
the synthesized rule (still in the constrained DSL), and proposes DRAFT candidates. Proposals
are validated: the DSL must accept the rule and simulation must show labelled precision at or
above the configured floor. Recurring benign patterns become bulk-policy proposals. Nothing an
agent proposes reaches production without replay, counterexamples, shadow mode and approval.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from asas.agents.prompts import DISCOVERY, PromptTemplate
from asas.agents.runtime import ActionRejected, Counters
from asas.agents.tools import ToolContext, ToolExecutor, tool_catalog
from asas.agents.validation import validate_prose
from asas.core.ids import stable_id
from asas.domain.models import Candidate, CandidateKind, Pattern, PatternKind, RuleSpec
from asas.engine.discovery import bulk_scope_for
from asas.engine.rules import evaluate_rule, validate_rule

DISCOVERY_TOOLS = frozenset(
    {
        "simulate_candidate_rule",
        "replay_rule",
        "find_similar_cases",
        "search_evidence_graph",
        "get_rule",
        "get_episode",
    }
)


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallTool(_A):
    action: Literal["call_tool"]
    tool: str
    args: dict[str, str]
    purpose: str = ""


class ProposeCandidate(_A):
    action: Literal["propose_candidate"]
    pattern_id: str
    rule: RuleSpec | None = None
    rationale: str = ""


class Skip(_A):
    action: Literal["skip"]
    pattern_id: str
    reason: str


class Finish(_A):
    action: Literal["finish"]


DiscoveryAction = Annotated[
    CallTool | ProposeCandidate | Skip | Finish, Field(discriminator="action")
]
_ADAPTER: TypeAdapter[DiscoveryAction] = TypeAdapter(DiscoveryAction)


class PatternState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern_id: str
    status: str = "PENDING"
    simulated: dict[str, str] = {}
    candidate: Candidate | None = None
    note: str = ""


class DiscoveryState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patterns: list[PatternState]
    finished: bool = False
    rejections: list[str] = []


class DiscoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str
    snapshot_id: str
    patterns: tuple[Pattern, ...]
    candidates: tuple[Candidate, ...]


def _signature(rule: RuleSpec) -> str:
    """Identity of a rule's logic, independent of its id and description."""
    return json.dumps(sorted(c.model_dump_json() for c in rule.conditions)) + json.dumps(
        rule.parameters, sort_keys=True
    )


class DiscoveryProgram:
    name = "discovery"
    version = "2.0"
    prompt: PromptTemplate = DISCOVERY
    tools = DISCOVERY_TOOLS
    state_type = DiscoveryState
    result_type = DiscoveryReport

    def __init__(self, patterns: list[Pattern], synthesized: Mapping[str, RuleSpec]) -> None:
        self.patterns = {p.pattern_id: p for p in patterns}
        self.synthesized = synthesized

    def start(self, subject: str, ctx: ToolContext) -> DiscoveryState:
        return DiscoveryState(patterns=[PatternState(pattern_id=p) for p in sorted(self.patterns)])

    def parse(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ADAPTER.validate_python(action).model_dump(mode="json")
        except Exception as exc:
            raise ActionRejected(str(exc)) from exc

    def playbook(self, state: DiscoveryState, ctx: ToolContext) -> dict[str, Any]:
        for ps in state.patterns:
            if ps.status != "PENDING":
                continue
            pattern = self.patterns[ps.pattern_id]
            if pattern.kind is PatternKind.RECURRING_BENIGN:
                return {
                    "action": "propose_candidate",
                    "pattern_id": ps.pattern_id,
                    "rule": None,
                    "rationale": "recurring, verified benign pattern with clean curated history",
                }
            rule = self.synthesized[ps.pattern_id]
            if not ps.simulated:
                return {
                    "action": "call_tool",
                    "tool": "simulate_candidate_rule",
                    "args": {"rule_json": rule.model_dump_json()},
                    "purpose": "measure the synthesized rule on history",
                }
            return {
                "action": "propose_candidate",
                "pattern_id": ps.pattern_id,
                "rule": rule.model_dump(mode="json"),
                "rationale": "uncaptured risk pattern; simplest rule keeping labelled precision",
            }
        return {"action": "finish"}

    def observe(self, state: DiscoveryState, ctx: ToolContext) -> dict[str, Any]:
        pending = [ps for ps in state.patterns if ps.status == "PENDING"][:5]
        return {
            "task": "turn validated patterns into candidate rules or bulk-policy proposals",
            "tools": tool_catalog(self.tools),
            "patterns": [
                {
                    "pattern_id": ps.pattern_id,
                    "kind": self.patterns[ps.pattern_id].kind.value,
                    "items": list(self.patterns[ps.pattern_id].items),
                    "synthesized_rule": (
                        self.synthesized[ps.pattern_id].model_dump(mode="json")
                        if ps.pattern_id in self.synthesized
                        else None
                    ),
                    "simulated": ps.simulated,
                }
                for ps in pending
            ],
            "rejections": state.rejections[-3:],
        }

    def apply(
        self,
        state: DiscoveryState,
        action: dict[str, Any],
        executor: ToolExecutor,
        step: int,
        ctx: ToolContext,
    ) -> tuple[DiscoveryState, dict[str, Any]]:
        parsed = _ADAPTER.validate_python(action)
        s = state.model_copy(deep=True)
        if isinstance(parsed, Finish):
            s.finished = True
            return s, {"finished": True}
        if isinstance(parsed, CallTool):
            result = executor.call(parsed.tool, parsed.args, step)
            if not result.ok:
                s.rejections.append(result.error)
                return s, {"tool_call": 1, "error": result.error}
            if parsed.tool == "simulate_candidate_rule":
                rule_id = json.loads(parsed.args.get("rule_json", "{}")).get("rule_id", "")
                for ps in s.patterns:
                    synth = self.synthesized.get(ps.pattern_id)
                    if synth is not None and synth.rule_id == rule_id:
                        ps.simulated = result.facts
            return s, {"tool_call": 1, "facts": result.facts}
        target = next((p for p in s.patterns if p.pattern_id == parsed.pattern_id), None)
        if target is None or target.status != "PENDING":
            s.rejections.append(f"unknown or closed pattern {parsed.pattern_id}")
            return s, {"rejected": "unknown pattern"}
        if isinstance(parsed, Skip):
            target.status, target.note = "SKIPPED", "skipped by agent"
            return s, {"skipped": target.pattern_id}
        return s, self._propose(s, target, parsed, ctx)

    def _propose(
        self, s: DiscoveryState, target: PatternState, a: ProposeCandidate, ctx: ToolContext
    ) -> dict[str, Any]:
        cfg = ctx.snapshot.cfg
        pattern = self.patterns[target.pattern_id]
        rationale = (
            a.rationale if not validate_prose(a.rationale, set()) else "pattern-derived proposal"
        )
        if pattern.kind is PatternKind.RECURRING_BENIGN:
            if a.rule is not None:
                s.rejections.append("benign patterns become bulk-policy proposals, not rules")
                return {"rejected": "rule not allowed for benign pattern"}
            scope = bulk_scope_for(pattern)
            candidate = Candidate(
                candidate_id=stable_id("CAND", "BULK", scope.hypothesis_type, scope.desk),
                kind=CandidateKind.BULK_POLICY,
                rule=None,
                bulk_scope=scope,
                pattern_id=pattern.pattern_id,
                rationale=rationale,
                proposed_by="agent:discovery",
                created_at=ctx.snapshot.as_of,
                base_bundle_id=ctx.snapshot.policy.bundle_id,
            )
        else:
            rule = a.rule or self.synthesized[pattern.pattern_id]
            covering = self._covered_by(s, pattern, ctx)
            if covering:
                target.status, target.note = "SKIPPED", f"already covered by {covering}"
                return {"skipped": "pattern already covered by an earlier proposal"}
            if _signature(rule) in self._known(s, ctx):
                s.rejections.append("rule duplicates an active rule or an earlier proposal")
                target.status, target.note = "SKIPPED", "duplicate of an existing rule"
                return {"rejected": "duplicate rule"}
            problems = validate_rule(rule, cfg)
            if problems:
                s.rejections.append(f"rule rejected by DSL: {problems}")
                return {"rejected": "invalid rule", "problems": problems}
            if ctx.simulate_rule is None:
                return {"rejected": "simulation unavailable"}
            sim = ctx.simulate_rule(rule).model_dump(mode="json")
            precision = sim.get("precision")
            floor = cfg.decimal("discovery", "min_candidate_precision")
            if precision is None or Decimal(str(precision)) < floor or int(sim.get("tp", 0)) < 1:
                s.rejections.append("rule rejected: simulated labelled precision below the floor")
                return {"rejected": "precision below floor", "simulation": sim}
            candidate = Candidate(
                candidate_id=stable_id("CAND", "RULE", rule.model_dump_json()),
                kind=CandidateKind.DETECTION_RULE,
                rule=rule,
                bulk_scope=None,
                pattern_id=pattern.pattern_id,
                rationale=rationale,
                proposed_by="agent:discovery",
                created_at=ctx.snapshot.as_of,
                base_bundle_id=ctx.snapshot.policy.bundle_id,
            )
        target.status, target.candidate = "PROPOSED", candidate
        return {"proposed": candidate.candidate_id}

    @staticmethod
    def _covered_by(s: DiscoveryState, pattern: Pattern, ctx: ToolContext) -> str | None:
        """An earlier proposal already fires on most of this pattern: a new rule adds nothing."""
        signals = ctx.snapshot.signal_set.signals
        sample = [signals[e] for e in pattern.sample_episode_ids if e in signals]
        limit = ctx.snapshot.cfg.decimal("discovery", "max_redundant_coverage")
        for ps in s.patterns:
            rule = ps.candidate.rule if ps.candidate is not None else None
            if rule is None or not sample:
                continue
            hit = sum(1 for sig in sample if evaluate_rule(rule, sig) is not None)
            if Decimal(hit) / Decimal(len(sample)) >= limit:
                return rule.subrule_id
        return None

    @staticmethod
    def _known(s: DiscoveryState, ctx: ToolContext) -> set[str]:
        known = {_signature(r) for r in ctx.snapshot.policy.ruleset.rules}
        known |= {
            _signature(ps.candidate.rule)
            for ps in s.patterns
            if ps.candidate is not None and ps.candidate.rule is not None
        }
        return known

    def done(self, state: DiscoveryState) -> bool:
        return state.finished or all(p.status != "PENDING" for p in state.patterns)

    def exhaust(self, state: DiscoveryState, reason: str) -> DiscoveryState:
        s = state.model_copy(deep=True)
        s.finished = True
        s.rejections.append(reason)
        return s

    def finalize(
        self, state: DiscoveryState, run_id: str, ctx: ToolContext, counters: Counters
    ) -> DiscoveryReport:
        unique = {
            ps.candidate.candidate_id: ps.candidate
            for ps in state.patterns
            if ps.candidate is not None
        }
        return DiscoveryReport(
            run_id=run_id,
            snapshot_id=ctx.snapshot.snapshot_id,
            patterns=tuple(self.patterns[p] for p in sorted(self.patterns)),
            candidates=tuple(unique[k] for k in sorted(unique)),
        )
