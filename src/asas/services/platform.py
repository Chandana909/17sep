"""Platform service: the one orchestration point used by the API, the CLI and the tests.

Pipeline: ingest -> point-in-time snapshot (episodes, signals, scores, detections) -> evidence
graph -> agentic investigation per case -> deterministic decisions -> cohorts. Around it:
detection challenge, pattern discovery, candidate replay / counterexamples / shadow, human
link confirmation, human outcomes and curation, governed release. Every step is audited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from asas.agents.challenger import ChallengeReport, ChallengerProgram
from asas.agents.discovery import DiscoveryProgram, DiscoveryReport
from asas.agents.gateway import (
    CachingGateway,
    CircuitBreaker,
    ModelGateway,
    OpenAICompatibleGateway,
    ResilientGateway,
)
from asas.agents.investigator import InvestigatorProgram
from asas.agents.memory import Memory
from asas.agents.runtime import AgentRuntime
from asas.agents.tools import ToolContext
from asas.core.config import Config
from asas.core.errors import AsasError, GovernanceError, PermissionDenied
from asas.core.ids import content_hash, stable_id
from asas.core.logging import get_logger, log_event
from asas.core.security import SYSTEM, Principal, Role, agent_principal, require_role
from asas.core.tracing import Span, Tracer
from asas.data.ingest import SourceBundle
from asas.domain.models import (
    BulkPolicy,
    BulkScope,
    CandidateKind,
    CandidateState,
    Case,
    Cohort,
    InvestigationResult,
    LabelQuality,
    LinkKind,
    LinkProposal,
    LinkStatus,
    LinkTier,
    OutcomeLabel,
    Pattern,
    PolicyBundle,
    ReviewOutcome,
    RuleSpec,
    TradeLink,
)
from asas.engine.challenge import candidate_findings
from asas.engine.decisions import case_id_for, decide_case, form_cohorts
from asas.engine.discovery import EpisodeFacts, episode_items, mine_patterns, synthesize_rule
from asas.engine.graph import EvidenceGraph, build_graph, neighborhood
from asas.engine.hypotheses import Assessment, assess
from asas.engine.rules import evaluate_ruleset, load_ruleset
from asas.engine.scoring import queue_key
from asas.engine.snapshot import Snapshot, build_snapshot
from asas.evolution.counterexamples import CounterexampleReport, attack_bulk, attack_detection
from asas.evolution.governance import Governance
from asas.evolution.replay import (
    BulkReplayReport,
    HistoryPoint,
    Metrics,
    ReplayReport,
    build_history,
    eval_times,
    metrics,
    replay_bulk,
    replay_detection,
)
from asas.evolution.shadow import ShadowReport, shadow_bulk, shadow_detection
from asas.store.db import Store, ts_key

_log = get_logger("platform")


class StoreSpanExporter:
    def __init__(self, store: Store) -> None:
        self._store = store

    def export(self, span: Span) -> None:
        self._store.insert_ignore(
            "spans",
            {
                "span_id": span.span_id,
                "trace_id": span.trace_id,
                "parent_span_id": span.parent_span_id,
                "name": span.name,
                "start_ns": span.start_ns,
                "end_ns": span.end_ns,
                "status": span.status,
                "attributes": json.dumps(span.attributes, default=str, sort_keys=True),
            },
        )


class PipelineReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_key: str
    as_of: datetime
    snapshot_id: str
    policy_bundle_id: str
    graph_version: str
    memory_version: str
    episodes: int
    cases: int
    proposed_bulk: int
    individual: int
    escalation: int
    abstained: int
    agent_failures: int
    cohorts: int
    unresolved_pairs: int
    case_ids: tuple[str, ...]
    cohort_list: tuple[Cohort, ...]


@dataclass
class _SnapshotBundle:
    snapshot: Snapshot
    assessments: dict[str, Assessment] | None = None
    graph: EvidenceGraph | None = None
    memory: Memory | None = None


def build_gateway(cfg: Config, store: Store) -> ModelGateway | None:
    if not cfg.boolean("agents", "enabled"):
        return None
    base = OpenAICompatibleGateway(
        base_url=cfg.string("agents", "base_url"),
        model_id=cfg.string("agents", "model_id"),
        api_key_env=cfg.string("agents", "api_key_env") or None,
        timeout_seconds=float(cfg.integer("agents", "timeout_seconds")),
    )
    return wrap_gateway(base, cfg, store)


def wrap_gateway(inner: ModelGateway, cfg: Config, store: Store) -> ModelGateway:
    breaker = CircuitBreaker(
        cfg.integer("agents", "breaker_threshold"),
        float(cfg.integer("agents", "breaker_reset_seconds")),
    )
    resilient = ResilientGateway(
        inner,
        cfg.integer("agents", "retries"),
        float(cfg.decimal("agents", "backoff_seconds")),
        breaker,
    )
    return CachingGateway(resilient, store)


class Platform:
    def __init__(
        self,
        store_path: str | Path,
        cfg: Config,
        gateway: ModelGateway | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = Store(store_path)
        self.tracer = tracer or Tracer([StoreSpanExporter(self.store)])
        self.governance = Governance(self.store, cfg)
        self.runtime = AgentRuntime(cfg, self.store, gateway, self.tracer)
        self._cache: dict[str, _SnapshotBundle] = {}

    # ------------------------------------------------------------ setup

    def seed_policy(self, ruleset_path: str | Path) -> PolicyBundle:
        ruleset = load_ruleset(ruleset_path, self.cfg)
        scopes = tuple(
            BulkScope(hypothesis_type=s["hypothesis_type"], desk=s["desk"])
            for s in self.cfg.require("policy", "initial_bulk_scopes")
        )
        return self.governance.seed(ruleset, BulkPolicy(allowed=scopes))

    def ingest(self, bundle: SourceBundle, principal: Principal) -> dict[str, int]:
        require_role(principal, Role.ADMIN, Role.SERVICE)
        counts = self.store.ingest(bundle)
        self.store.audit(principal.user_id, "INGEST", "source", counts)
        self._cache.clear()
        return counts

    # ------------------------------------------------------------ snapshots

    def _links(self, as_of: datetime) -> tuple[list[TradeLink], list[LinkProposal]]:
        confirmed: list[TradeLink] = []
        proposals: list[LinkProposal] = []
        for _, _, payload in self.store.list_artifacts("link_proposal"):
            p = LinkProposal.model_validate_json(payload)
            if p.created_at > as_of:
                continue
            proposals.append(p)
            if p.status is LinkStatus.CANONICAL:
                confirmed.append(
                    TradeLink(
                        src=p.trade_b,
                        dst=p.trade_a,
                        kind=LinkKind.AGENT_LINK,
                        tier=LinkTier.AGENT,
                        status=LinkStatus.CANONICAL,
                        provenance=f"human-confirmed:{p.proposal_id}",
                        reason="CONFIRMED_AGENT_LINK",
                        evidence=dict(p.verification),
                    )
                )
        return confirmed, proposals

    def _bundle(self, as_of: datetime, history: bool = False) -> _SnapshotBundle:
        policy = self.governance.active_bundle()
        confirmed, proposals = self._links(as_of)
        # pinned per confirmed-link set: proposals made during a run never shift its snapshot
        key = content_hash([as_of, history, policy.bundle_id, [x.model_dump() for x in confirmed]])
        if key not in self._cache:
            with self.tracer.span("snapshot.build", as_of=as_of.isoformat(), history=history):
                source = self.store.load_bundle(as_of)
                snap = build_snapshot(source, self.cfg, policy, as_of, confirmed, proposals)
                if history:
                    snap = build_snapshot(
                        source,
                        self.cfg,
                        policy,
                        as_of,
                        confirmed,
                        proposals,
                        eval_times=eval_times(snap, self.cfg),
                    )
            self._cache[key] = _SnapshotBundle(snap)
        return self._cache[key]

    def snapshot(self, as_of: datetime) -> Snapshot:
        return self._bundle(as_of).snapshot

    def assessments(self, as_of: datetime, history: bool = False) -> dict[str, Assessment]:
        b = self._bundle(as_of, history)
        if b.assessments is None:
            with self.tracer.span("assess.all", as_of=as_of.isoformat()):
                b.assessments = {
                    e.episode_id: assess(b.snapshot, e.episode_id, SYSTEM)
                    for e in b.snapshot.episodes
                }
        return b.assessments

    def graph(self, as_of: datetime) -> EvidenceGraph:
        b = self._bundle(as_of)
        if b.graph is None:
            _, proposals = self._links(as_of)
            patterns = [
                Pattern.model_validate_json(p) for _, _, p in self.store.list_artifacts("pattern")
            ]
            b.graph = build_graph(b.snapshot, proposals, patterns)
            self.store.put_artifact(
                "graph_version",
                b.snapshot.snapshot_id,
                b.graph.version,
                {
                    "version": b.graph.version,
                    "nodes": len(b.graph.nodes),
                    "edges": len(b.graph.edges),
                },
            )
        return b.graph

    def memory(self, as_of: datetime) -> Memory:
        b = self._bundle(as_of)
        if b.memory is None:
            investigations = {r.episode_id: r for r in self._investigations()}
            patterns = [
                Pattern.model_validate_json(p) for _, _, p in self.store.list_artifacts("pattern")
            ]
            b.memory = Memory(b.snapshot, investigations, patterns)
        return b.memory

    # ------------------------------------------------------------ tool services

    def history_points(self, as_of: datetime, start: datetime, end: datetime) -> list[HistoryPoint]:
        b = self._bundle(as_of, history=True)
        return build_history(b.snapshot, self.assessments(as_of, history=True), start, end)

    def windows(self, as_of: datetime) -> tuple[datetime, datetime, datetime]:
        shadow_days = self.cfg.integer("shadow", "window_days")
        replay_days = self.cfg.integer("replay", "window_days")
        shadow_start = as_of - timedelta(days=shadow_days)
        return shadow_start - timedelta(days=replay_days), shadow_start, as_of

    def simulate(self, rule: RuleSpec, as_of: datetime) -> Metrics:
        start, shadow_start, _ = self.windows(as_of)
        points = self.history_points(as_of, start, shadow_start)
        return metrics(points, lambda p: bool(evaluate_ruleset([rule], p.signals)))

    def replay_existing(self, rule_id: str, as_of: datetime) -> Metrics:
        rules = [r for r in self.governance.active_bundle().ruleset.rules if r.rule_id == rule_id]
        if not rules:
            raise GovernanceError(f"rule {rule_id} is not a production DSL rule")
        start, shadow_start, _ = self.windows(as_of)
        points = self.history_points(as_of, start, shadow_start)
        return metrics(points, lambda p: bool(evaluate_ruleset(rules, p.signals)))

    def tool_context(self, as_of: datetime, principal: Principal, agent: str) -> ToolContext:
        snap = self.snapshot(as_of)
        graph_depth = self.cfg.integer("tools", "graph_max_depth")
        graph_nodes = self.cfg.integer("tools", "graph_max_nodes")
        return ToolContext(
            snapshot=snap,
            principal=agent_principal(agent, principal),
            graph_neighborhood=lambda node, depth: neighborhood(
                self.graph(as_of), node, min(depth, graph_depth), graph_nodes
            ),
            similar_cases=lambda eid, k: self.memory(as_of).similar_cases(
                eid, min(k, self.cfg.integer("tools", "similar_k"))
            ),
            replay_rule=lambda rule_id: self.replay_existing(rule_id, as_of),
            simulate_rule=lambda rule: self.simulate(rule, as_of),
        )

    # ------------------------------------------------------------ investigation + pipeline

    def investigate(
        self, episode_id: str, as_of: datetime, principal: Principal
    ) -> InvestigationResult:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        ctx = self.tool_context(as_of, principal, "investigator")
        outcome = self.runtime.run(InvestigatorProgram(case_id_for(episode_id)), episode_id, ctx)
        result = outcome.result
        if not outcome.replayed:
            for p in result.link_proposals:
                if self.store.get_artifact("link_proposal", p.proposal_id) is None:
                    self.store.put_artifact("link_proposal", p.proposal_id, "1", p)
                    self.store.audit(
                        p.proposed_by,
                        f"LINK_{p.status.value}",
                        p.proposal_id,
                        {"trade_a": p.trade_a, "trade_b": p.trade_b},
                    )
            self.store.put_artifact("memory_episodic", episode_id, result.run_id, result)
        return result

    def resolve_links(self, as_of: datetime, principal: Principal) -> list[LinkProposal]:
        """Agentic pass over every relationship deterministic linking could not prove: the
        investigator examines each episode holding an unresolved cancelled trade and proposes
        links, which are verified deterministically and stay quarantined until confirmed."""
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        snap = self.snapshot(as_of)
        proposals: list[LinkProposal] = []
        for episode_id in sorted({p.episode_a for p in snap.linking.unresolved}):
            result = self.investigate(episode_id, as_of, principal)
            proposals.extend(result.link_proposals)
        self.store.audit(
            principal.user_id,
            "LINK_RESOLUTION_RUN",
            snap.snapshot_id,
            {
                "proposals": len(proposals),
                "verified": sum(p.status is LinkStatus.VERIFIED for p in proposals),
            },
        )
        return proposals

    def run_pipeline(self, as_of: datetime, principal: Principal) -> PipelineReport:
        require_role(principal, Role.ADMIN, Role.SERVICE, Role.INVESTIGATOR)
        with self.tracer.span("pipeline.run", as_of=as_of.isoformat()):
            snap = self.snapshot(as_of)
            graph = self.graph(as_of)
            policy = snap.policy
            decided: list[Case] = []
            failures = 0
            review_start = as_of - timedelta(
                days=self.cfg.integer("pipeline", "review_window_days")
            )
            for ep in snap.episodes:
                if not any(snap.alerts_by_id[a].alert_time > review_start for a in ep.alert_ids):
                    continue
                result: InvestigationResult | None = None
                error: str | None = None
                try:
                    result = self.investigate(ep.episode_id, as_of, principal)
                except AsasError as exc:
                    error = type(exc).__name__
                except Exception as exc:  # any agent failure falls back to individual review
                    error = type(exc).__name__
                    log_event(_log, "investigation.failed", episode_id=ep.episode_id, error=error)
                if error:
                    failures += 1
                decided.append(
                    decide_case(
                        ep,
                        snap.scores[ep.episode_id],
                        result,
                        error,
                        policy.bulk_policy,
                        self.cfg,
                        as_of,
                    )
                )
            cases, cohorts = form_cohorts(decided, self.cfg)
            cases.sort(key=lambda c: queue_key(c.score, snap.episodes_by_id[c.episode_id]))
            run_key = stable_id("PIPE", snap.snapshot_id, as_of.isoformat())
            for c in cases:
                self.store.put_artifact("case", c.case_id, run_key, c)
            for coh in cohorts:
                self.store.put_artifact("cohort", coh.cohort_id, run_key, coh)
            memory = self.memory(as_of)
            report = PipelineReport(
                run_key=run_key,
                as_of=as_of,
                snapshot_id=snap.snapshot_id,
                policy_bundle_id=policy.bundle_id,
                graph_version=graph.version,
                memory_version=memory.version,
                episodes=len(snap.episodes),
                cases=len(cases),
                proposed_bulk=sum(c.recommendation.value == "PROPOSED_BULK" for c in cases),
                individual=sum(c.recommendation.value == "INDIVIDUAL_REVIEW" for c in cases),
                escalation=sum(c.recommendation.value == "ESCALATION_RECOMMENDED" for c in cases),
                abstained=sum(any(r.startswith("ABSTAINED") for r in c.reasons) for c in cases),
                agent_failures=failures,
                cohorts=len(cohorts),
                unresolved_pairs=len(snap.linking.unresolved),
                case_ids=tuple(c.case_id for c in cases),
                cohort_list=tuple(cohorts),
            )
            self.store.put_artifact("pipeline_run", "latest", run_key, report)
            self.store.audit(
                principal.user_id,
                "PIPELINE_RUN",
                run_key,
                {
                    "cases": report.cases,
                    "bulk": report.proposed_bulk,
                    "escalation": report.escalation,
                    "policy": policy.bundle_id,
                },
            )
            log_event(_log, "pipeline.completed", run_key=run_key, cases=report.cases)
            return report

    # ------------------------------------------------------------ challenge and discovery

    def challenge(self, as_of: datetime, principal: Principal) -> ChallengeReport:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        snap = self.snapshot(as_of)
        assessments = self.assessments(as_of)
        review_start = as_of - timedelta(days=self.cfg.integer("pipeline", "review_window_days"))
        candidates = [
            c
            for c in candidate_findings(snap, assessments)
            if snap.episodes_by_id[c.episode_id].end >= review_start
        ]
        limit = self.cfg.integer("challenger", "max_findings_per_run")
        order = ("BLIND_SPOT", "MISSING_CONTEXT", "ALTERNATIVE_EXPLANATION", "REDUNDANT_DETECTION")
        queues = {
            k: sorted((c for c in candidates if c.kind.value == k), key=lambda c: c.finding_id)
            for k in order
        }
        chosen: list[Any] = []
        while len(chosen) < limit and any(queues.values()):
            for kind in order:  # round-robin in priority order so every kind is examined
                if queues[kind] and len(chosen) < limit:
                    chosen.append(queues[kind].pop(0))
        ctx = self.tool_context(as_of, principal, "challenger")
        outcome = self.runtime.run(
            ChallengerProgram(chosen, assessments), f"challenge:{snap.snapshot_id}", ctx
        )
        self.store.put_artifact("challenge_report", "latest", outcome.result.run_id, outcome.result)
        self.store.audit(
            principal.user_id,
            "CHALLENGE_RUN",
            outcome.result.run_id,
            {"findings": len(outcome.result.findings)},
        )
        return outcome.result

    def discover(self, as_of: datetime, principal: Principal) -> DiscoveryReport:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        start, shadow_start, _ = self.windows(as_of)
        b = self._bundle(as_of, history=True)
        assessments = self.assessments(as_of, history=True)
        policy = b.snapshot.policy
        facts: list[EpisodeFacts] = []
        for p in build_history(b.snapshot, assessments, start, shadow_start):
            detected = bool(evaluate_ruleset(policy.ruleset.rules, p.signals))
            facts.append(
                EpisodeFacts(
                    p.episode_id,
                    p.desk,
                    episode_items(p.signals, self.cfg),
                    p.label,
                    p.assessed_class,
                    p.assessed_type,
                    detected,
                )
            )
        patterns = mine_patterns(facts, policy.bulk_policy, self.cfg)
        signals = b.snapshot.signal_set.signals
        synthesized = {
            pat.pattern_id: synthesize_rule(pat, facts, signals, self.cfg)
            for pat in patterns
            if pat.kind.value == "UNCAPTURED_RISK"
        }
        for pat in patterns:
            self.store.put_artifact("pattern", pat.pattern_id, "1", pat)
        ctx = self.tool_context(as_of, principal, "discovery")
        outcome = self.runtime.run(
            DiscoveryProgram(patterns, synthesized), f"discover:{b.snapshot.snapshot_id}", ctx
        )
        for candidate in outcome.result.candidates:
            self.governance.create(candidate)
        self.store.put_artifact("discovery_report", "latest", outcome.result.run_id, outcome.result)
        self.store.audit(
            principal.user_id,
            "DISCOVERY_RUN",
            outcome.result.run_id,
            {"patterns": len(patterns), "candidates": len(outcome.result.candidates)},
        )
        return outcome.result

    # ------------------------------------------------------------ candidate evaluation

    def replay_candidate(
        self, candidate_id: str, as_of: datetime, principal: Principal
    ) -> ReplayReport | BulkReplayReport:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        c = self.governance.candidate(candidate_id)
        start, shadow_start, _ = self.windows(as_of)
        points = self.history_points(as_of, start, shadow_start)
        sample = self.cfg.integer("replay", "sample_size")
        report: ReplayReport | BulkReplayReport
        if c.kind is CandidateKind.DETECTION_RULE and c.rule is not None:
            report = replay_detection(
                c.rule, self.governance.active_bundle(), points, start, shadow_start, sample
            )
        elif c.bulk_scope is not None:
            report = replay_bulk(c.bulk_scope, points, start, shadow_start, sample)
        else:
            raise GovernanceError("candidate has no payload")
        self.governance.record_step(
            candidate_id,
            CandidateState.REPLAYED,
            principal.user_id,
            "replay_report",
            report.report_id,
            report,
            "historical replay",
        )
        return report

    def attack_candidate(
        self, candidate_id: str, as_of: datetime, principal: Principal
    ) -> CounterexampleReport:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        c = self.governance.candidate(candidate_id)
        start, shadow_start, _ = self.windows(as_of)
        points = self.history_points(as_of, start, shadow_start)
        if c.kind is CandidateKind.DETECTION_RULE and c.rule is not None:
            report = attack_detection(c.rule, points, self.cfg)
        elif c.bulk_scope is not None:
            report = attack_bulk(c.bulk_scope, points, self.cfg)
        else:
            raise GovernanceError("candidate has no payload")
        state = (
            CandidateState.COUNTEREXAMPLES_PASSED
            if report.passed
            else CandidateState.COUNTEREXAMPLES_FAILED
        )
        self.governance.record_step(
            candidate_id,
            state,
            principal.user_id,
            "counterexample_report",
            report.report_id,
            report,
            "; ".join(report.failures) or "passed",
        )
        return report

    def shadow_candidate(
        self, candidate_id: str, as_of: datetime, principal: Principal
    ) -> ShadowReport:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER, Role.SERVICE)
        c = self.governance.candidate(candidate_id)
        _, shadow_start, end = self.windows(as_of)
        points = self.history_points(as_of, shadow_start, end + timedelta(microseconds=1))
        if c.kind is CandidateKind.DETECTION_RULE and c.rule is not None:
            report = shadow_detection(
                c.rule, self.governance.active_bundle(), points, shadow_start, end, self.cfg
            )
        elif c.bulk_scope is not None:
            report = shadow_bulk(c.bulk_scope, points, shadow_start, end, self.cfg)
        else:
            raise GovernanceError("candidate has no payload")
        state = CandidateState.SHADOW_PASSED if report.passed else CandidateState.SHADOW_FAILED
        self.governance.record_step(
            candidate_id,
            state,
            principal.user_id,
            "shadow_report",
            report.report_id,
            report,
            "; ".join(report.failures) or "passed",
        )
        return report

    def evaluate_candidate(
        self, candidate_id: str, as_of: datetime, principal: Principal
    ) -> CandidateState:
        """Run the full evidence chain, stopping at the first failed gate."""
        self.replay_candidate(candidate_id, as_of, principal)
        if not self.attack_candidate(candidate_id, as_of, principal).passed:
            return self.governance.state(candidate_id)
        self.shadow_candidate(candidate_id, as_of, principal)
        return self.governance.state(candidate_id)

    def submit(self, candidate_id: str, principal: Principal, note: str) -> CandidateState:
        self.governance.submit(candidate_id, principal, note)
        return self.governance.state(candidate_id)

    def approve(self, candidate_id: str, principal: Principal, note: str) -> PolicyBundle:
        bundle = self.governance.approve(candidate_id, principal, note)
        self._cache.clear()
        return bundle

    def reject(self, candidate_id: str, principal: Principal, reason: str) -> CandidateState:
        self.governance.reject(candidate_id, principal, reason)
        return self.governance.state(candidate_id)

    def rollback(self, bundle_id: str, principal: Principal, reason: str) -> PolicyBundle:
        bundle = self.governance.rollback(bundle_id, principal, reason)
        self._cache.clear()
        return bundle

    # ------------------------------------------------------------ human actions

    def confirm_link(self, proposal_id: str, principal: Principal) -> LinkProposal:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER)
        if principal.user_id.startswith("agent:"):
            raise PermissionDenied("links become canonical only by human confirmation")
        current = self.store.get_model("link_proposal", proposal_id, LinkProposal)
        if current is None:
            raise GovernanceError(f"unknown proposal {proposal_id}")
        if current.status is not LinkStatus.VERIFIED:
            raise GovernanceError(
                f"only VERIFIED proposals can be confirmed (is {current.status.value})"
            )
        confirmed = current.model_copy(
            update={
                "status": LinkStatus.CANONICAL,
                "verification": {**current.verification, "confirmed_by": principal.user_id},
            }
        )
        self.store.put_artifact("link_proposal", proposal_id, "2", confirmed)
        self.store.audit(
            principal.user_id,
            "LINK_CONFIRMED",
            proposal_id,
            {"trade_a": current.trade_a, "trade_b": current.trade_b},
        )
        self._cache.clear()
        return confirmed

    def record_decision(
        self, case_id: str, label: OutcomeLabel, principal: Principal, decided_at: datetime
    ) -> list[ReviewOutcome]:
        require_role(principal, Role.INVESTIGATOR, Role.APPROVER)
        case = self.case(case_id)
        outcomes = [
            ReviewOutcome(
                outcome_id=stable_id("OUT", a, principal.user_id, ts_key(decided_at)),
                alert_id=a,
                label=label,
                quality=LabelQuality.RAW,
                decided_at=decided_at,
                decided_by=principal.user_id,
            )
            for a in case.alert_ids
        ]
        self.store.ingest(SourceBundle(outcomes=tuple(outcomes)))
        self.store.audit(principal.user_id, "CASE_DECISION", case_id, {"label": label.value})
        self._cache.clear()
        return outcomes

    def curate(self, outcome_id: str, principal: Principal) -> ReviewOutcome:
        require_role(principal, Role.APPROVER)
        rows = self.store.query("SELECT payload FROM outcomes WHERE outcome_id = ?", (outcome_id,))
        if not rows:
            raise GovernanceError(f"unknown outcome {outcome_id}")
        raw = ReviewOutcome.model_validate_json(rows[0][0])
        if raw.decided_by == principal.user_id:
            raise PermissionDenied(
                "four-eyes: curation must be done by someone other than the decider"
            )
        curated = raw.model_copy(
            update={"outcome_id": f"C-{raw.outcome_id}", "quality": LabelQuality.CURATED}
        )
        self.store.ingest(SourceBundle(outcomes=(curated,)))
        self.store.audit(principal.user_id, "OUTCOME_CURATED", curated.outcome_id, {})
        self._cache.clear()
        return curated

    # ------------------------------------------------------------ queries

    def latest_run(self) -> PipelineReport | None:
        return self.store.get_model("pipeline_run", "latest", PipelineReport)

    def case(self, case_id: str) -> Case:
        run = self.latest_run()
        found = self.store.get_model("case", case_id, Case, run.run_key if run else None)
        if found is None:
            raise GovernanceError(f"unknown case {case_id}")
        return found

    def cases(self) -> list[Case]:
        run = self.latest_run()
        if run is None:
            return []
        out = []
        for cid in run.case_ids:
            c = self.store.get_model("case", cid, Case, run.run_key)
            if c is not None:
                out.append(c)
        return out

    def investigation(self, run_id: str) -> InvestigationResult | None:
        rows = self.store.query("SELECT result FROM agent_run_results WHERE run_id = ?", (run_id,))
        return InvestigationResult.model_validate_json(rows[0][0]) if rows else None

    def _investigations(self) -> list[InvestigationResult]:
        return [
            InvestigationResult.model_validate_json(p)
            for _, _, p in self.store.list_artifacts("memory_episodic")
        ]

    def runs_for(self, subject: str) -> list[str]:
        rows = self.store.query(
            "SELECT run_id FROM agent_runs WHERE subject = ? ORDER BY started_at DESC", (subject,)
        )
        return [str(r[0]) for r in rows]

    def run_trace(self, run_id: str) -> dict[str, Any]:
        manifest = self.store.query("SELECT manifest FROM agent_runs WHERE run_id = ?", (run_id,))
        steps = self.store.query(
            "SELECT step, policy, action, observation FROM agent_steps "
            "WHERE run_id = ? ORDER BY step",
            (run_id,),
        )
        calls = self.store.query(
            "SELECT call_id, step, tool, args, cached, duration_ms, error FROM tool_calls "
            "WHERE run_id = ? ORDER BY step",
            (run_id,),
        )
        return {
            "manifest": json.loads(manifest[0][0]) if manifest else None,
            "steps": [
                {"step": s, "policy": p, "action": json.loads(a), "observation": json.loads(o)}
                for s, p, a, o in steps
            ],
            "tool_calls": [
                {
                    "call_id": c,
                    "step": s,
                    "tool": t,
                    "args": json.loads(a),
                    "cached": bool(k),
                    "duration_ms": d,
                    "error": e,
                }
                for c, s, t, a, k, d, e in calls
            ],
        }

    def link_proposals(self) -> list[LinkProposal]:
        return [
            LinkProposal.model_validate_json(p)
            for _, _, p in self.store.list_artifacts("link_proposal")
        ]

    def patterns(self) -> list[Pattern]:
        return [Pattern.model_validate_json(p) for _, _, p in self.store.list_artifacts("pattern")]

    def candidate_view(self, candidate_id: str) -> dict[str, Any]:
        c = self.governance.candidate(candidate_id)
        artifacts: dict[str, Any] = {}
        for kind in ("replay_report", "counterexample_report", "shadow_report"):
            for key, _, payload in self.store.list_artifacts(kind):
                data = json.loads(payload)
                subject = data.get("subject")
                expected = (
                    c.rule.subrule_id
                    if c.rule
                    else (
                        f"{c.bulk_scope.hypothesis_type}@{c.bulk_scope.desk}"
                        if c.bulk_scope
                        else ""
                    )
                )
                if subject == expected:
                    data.pop("decisions", None)
                    artifacts[kind] = data | {"report_id": key}
        return {
            "candidate": c.model_dump(mode="json"),
            "state": self.governance.state(candidate_id).value,
            "events": [e.model_dump(mode="json") for e in self.governance.events(candidate_id)],
            "artifacts": artifacts,
        }
