"""End-to-end scenarios over the full lifecycle (one demo run per session).

1. multiple alerts collapse into one episode      7. candidate replayed historically
2. agent investigates a case dynamically          8. counterexamples are found
3. agent discovers a relationship linking missed  9. candidate runs in shadow mode
4. agent challenges an existing rule             10. human approval promotes a version
5. agent discovers an uncaptured pattern         11. insufficient evidence -> abstention
6. candidate rule is generated                   12. agent/model failure falls back safely
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from asas.agents.gateway import ModelRequest, ScriptedGateway
from asas.core.config import Config
from asas.core.errors import GovernanceError, ModelError, PermissionDenied
from asas.core.security import SYSTEM, Principal, Role
from asas.data.synthetic import INJECTION_TEXT, SyntheticDataset
from asas.domain.models import (
    CandidateKind,
    CandidateState,
    Condition,
    FindingKind,
    LinkKind,
    LinkStatus,
    Op,
    PatternKind,
    Recommendation,
    RuleSpec,
)
from asas.engine.rules import evaluate_ruleset, validate_rule
from asas.evolution.counterexamples import attack_detection
from asas.services.demo import ADMIN, ANALYST, APPROVER, DemoResult, ruleset_path
from asas.services.platform import Platform, wrap_gateway


def _scenario(demo: DemoResult, episode_trades: tuple[str, ...]) -> str:
    return demo.dataset.truth.scenario_by_trade.get(episode_trades[0], "?")


def test_01_multiple_alerts_collapse_into_one_episode(demo: DemoResult) -> None:
    snap = demo.platform.snapshot(demo.dataset.end)
    multi = [e for e in snap.episodes if len(e.alert_ids) >= 2 and len(e.trade_ids) >= 2]
    assert multi, "cancel/rebook lifecycles fire several alerts on several trades"
    rules = {snap.alerts_by_id[a].rule_id for e in multi for a in e.alert_ids}
    assert {"R200", "R400"} <= rules
    assert all(
        any(x.kind is LinkKind.REBOOK_OF for x in e.links)
        or any(x.kind is LinkKind.AGENT_LINK for x in e.links)
        for e in multi
    )


def test_02_agent_investigates_dynamically(demo: DemoResult) -> None:
    investigations = [
        demo.platform.investigation(c.investigation_run_id)
        for c in demo.platform.cases()
        if c.investigation_run_id
    ]
    by_conclusion: dict[str, set[str]] = {}
    for r in investigations:
        if r and r.conclusion:
            by_conclusion.setdefault(r.conclusion, set()).update(e.tool for e in r.evidence)
    assert by_conclusion["PRICE_CORRECTION"] != by_conclusion["CANCEL_REBOOK_CORRECTION"]
    assert "compare_trades" in by_conclusion["CANCEL_REBOOK_CORRECTION"]
    assert "get_peer_comparison" in by_conclusion["OFF_MARKET_AMENDMENT"]
    assert "compare_trades" not in by_conclusion["PRICE_CORRECTION"], "no evidence it does not need"
    for r in investigations:
        assert r is not None
        if r.conclusion:
            concluded = next(h for h in r.hypotheses if h.type == r.conclusion)
            assert concluded.evidence_ids, "every conclusion cites retrieved evidence"
        assert len(r.evidence) <= 24, "only the evidence the hypotheses needed"


def test_03_agent_discovers_relationships_linking_missed(demo: DemoResult) -> None:
    assert demo.confirmed_links
    linking = demo.evaluation.linking
    assert float(linking["after_recall"]) > float(linking["before_recall"])
    assert linking["after_precision"] == "1.0000"
    proposals = {p.proposal_id: p for p in demo.platform.link_proposals()}
    confirmed = [proposals[i] for i in demo.confirmed_links]
    assert all(
        p.status is LinkStatus.CANONICAL and p.verification["confirmed_by"] == ANALYST.user_id
        for p in confirmed
    )
    snap = demo.platform.snapshot(demo.dataset.end)
    agent_links = [x for e in snap.episodes for x in e.links if x.kind is LinkKind.AGENT_LINK]
    assert agent_links and all(x.provenance.startswith("human-confirmed:") for x in agent_links)


def test_03b_agent_links_are_quarantined_until_confirmed(demo: DemoResult) -> None:
    with pytest.raises(PermissionDenied):
        demo.platform.confirm_link(
            demo.confirmed_links[0],
            Principal(user_id="agent:investigator", roles=frozenset({Role.INVESTIGATOR})),
        )
    with pytest.raises(GovernanceError):  # already canonical: cannot be re-confirmed
        demo.platform.confirm_link(demo.confirmed_links[0], ANALYST)


def test_04_agent_challenges_existing_rules(demo: DemoResult) -> None:
    confirmed = Counter(f.kind for f in demo.challenge.findings if f.status == "CONFIRMED")
    assert confirmed[FindingKind.BLIND_SPOT] > 0
    assert confirmed[FindingKind.ALTERNATIVE_EXPLANATION] > 0
    assert confirmed[FindingKind.MISSING_CONTEXT] > 0, "R400 fires on like-for-like rebooks"
    assert confirmed[FindingKind.REDUNDANT_DETECTION] > 0
    blind = [
        f
        for f in demo.challenge.findings
        if f.kind is FindingKind.BLIND_SPOT and f.status == "CONFIRMED"
    ]
    snap = demo.platform.snapshot(demo.dataset.end)
    assert all(
        _scenario(demo, snap.episodes_by_id[f.episode_id].trade_ids) == "WINDOW_DRESSING"
        for f in blind
    )


def test_05_uncaptured_pattern_is_discovered(demo: DemoResult) -> None:
    risk = [p for p in demo.discovery.patterns if p.kind is PatternKind.UNCAPTURED_RISK]
    assert risk and all(p.rule_coverage <= 0.2 and p.escalated >= 3 for p in risk)
    benign = [p for p in demo.discovery.patterns if p.kind is PatternKind.RECURRING_BENIGN]
    assert benign and all(p.escalated == 0 for p in benign)


def test_06_candidate_rule_is_generated_in_the_constrained_dsl(
    demo: DemoResult, cfg: Config
) -> None:
    rules = [c for c in demo.discovery.candidates if c.kind is CandidateKind.DETECTION_RULE]
    assert rules
    for c in rules:
        assert c.rule is not None and validate_rule(c.rule, cfg) == []
        assert c.proposed_by == "agent:discovery"
    assert any(c.kind is CandidateKind.BULK_POLICY for c in demo.discovery.candidates)


def _artifact(demo: DemoResult, candidate_id: str, kind: str) -> dict:  # type: ignore[type-arg]
    view = demo.platform.candidate_view(candidate_id)
    return view["artifacts"][kind]  # type: ignore[no-any-return]


def test_07_candidate_is_replayed_historically(demo: DemoResult) -> None:
    rule = next(c for c in demo.discovery.candidates if c.kind is CandidateKind.DETECTION_RULE)
    replay = _artifact(demo, rule.candidate_id, "replay_report")
    assert replay["added_positive"] > 0 and replay["lost"] == 0
    assert (
        replay["candidate"]["precision"] is not None
        and float(replay["candidate"]["precision"]) >= 0.8
    )


def test_08_counterexamples_are_found_and_bad_rules_fail(demo: DemoResult, cfg: Config) -> None:
    rule = next(c for c in demo.discovery.candidates if c.kind is CandidateKind.DETECTION_RULE)
    report = _artifact(demo, rule.candidate_id, "counterexample_report")
    assert report["passed"] and not report["failures"]
    assert report["counts"].get("FALSE_POSITIVE", 0) == 0
    naive = RuleSpec(
        rule_id="DRNAIVE",
        subrule_id="DRNAIVE.1",
        description="any amendment",
        severity="MEDIUM",
        conditions=(Condition(signal="has_amend", op=Op.EQ, value="true"),),
        parameters={},
    )
    start, shadow_start, _ = demo.platform.windows(demo.dataset.end)
    points = demo.platform.history_points(demo.dataset.end, start, shadow_start)
    attacked = attack_detection(naive, points, cfg)
    assert not attacked.passed and attacked.counts.get("FALSE_POSITIVE", 0) > 0


def test_09_candidate_runs_in_shadow_without_touching_production(demo: DemoResult) -> None:
    rule = next(c for c in demo.discovery.candidates if c.kind is CandidateKind.DETECTION_RULE)
    shadow = _artifact(demo, rule.candidate_id, "shadow_report")
    assert shadow["passed"] and shadow["candidate_only"] > 0
    assert shadow["candidate_only_assessed_anomalous"] == shadow["candidate_only"]
    events = [e.state for e in demo.platform.governance.events(rule.candidate_id)]
    assert events[:4] == [
        CandidateState.DRAFT,
        CandidateState.REPLAYED,
        CandidateState.COUNTEREXAMPLES_PASSED,
        CandidateState.SHADOW_PASSED,
    ]


def test_10_human_approval_promotes_a_version(demo: DemoResult) -> None:
    assert len(demo.released) >= 2
    bundles = {b.bundle_id: b for b in demo.platform.governance.bundles()}
    active = demo.platform.governance.active_bundle()
    assert active.version == max(b.version for b in bundles.values()) and active.parent_id
    rule_release = next(
        b
        for b in demo.released
        if b.source_candidate and any(r.rule_id.startswith("DR") for r in b.ruleset.rules)
    )
    events = demo.platform.governance.events(rule_release.source_candidate or "")
    submitter = next(e.actor for e in events if e.state is CandidateState.AWAITING_APPROVAL)
    approver = next(e.actor for e in events if e.state is CandidateState.RELEASED)
    assert submitter == ANALYST.user_id and approver == APPROVER.user_id != submitter
    snap = demo.platform.snapshot(demo.dataset.end)
    new_rules = [r for r in active.ruleset.rules if r.rule_id.startswith("DR")]
    hits = [
        e
        for e in snap.episodes
        if evaluate_ruleset(new_rules, snap.signal_set.signals[e.episode_id])
    ]
    assert hits and all(demo.dataset.truth.is_risky(e.trade_ids[0]) for e in hits)
    assert sum(_scenario(demo, e.trade_ids) == "WINDOW_DRESSING" for e in hits) >= len(hits) * 0.9
    assert demo.final_run.proposed_bulk > demo.second_run.proposed_bulk, (
        "approved bulk scope applies"
    )
    assert demo.platform.store.verify_audit_chain()[0]


def test_10b_governance_cannot_be_bypassed(demo: DemoResult, cfg: Config) -> None:
    platform = demo.platform
    rule = RuleSpec(
        rule_id="DRMANUAL",
        subrule_id="DRMANUAL.1",
        description="manual",
        severity="LOW",
        conditions=(Condition(signal="has_cancel", op=Op.EQ, value="true"),),
        parameters={},
    )
    from asas.domain.models import Candidate

    candidate = platform.governance.create(
        Candidate(
            candidate_id="CAND-MANUAL",
            kind=CandidateKind.DETECTION_RULE,
            rule=rule,
            bulk_scope=None,
            pattern_id="none",
            rationale="test",
            proposed_by="agent:discovery",
            created_at=demo.dataset.end,
            base_bundle_id=platform.governance.active_bundle().bundle_id,
        )
    )
    with pytest.raises(GovernanceError):  # cannot skip replay/counterexamples/shadow
        platform.submit(candidate.candidate_id, ANALYST, "skip ahead")
    with pytest.raises(PermissionDenied):  # agents cannot act on governance
        platform.approve(
            candidate.candidate_id,
            Principal(user_id="agent:discovery", roles=frozenset({Role.SERVICE})),
            "self-approve",
        )


def test_11_insufficient_evidence_causes_abstention(demo: DemoResult) -> None:
    snap = demo.platform.snapshot(demo.dataset.end)
    late = [
        c
        for c in demo.platform.cases()
        if _scenario(demo, snap.episodes_by_id[c.episode_id].trade_ids) == "LATE_BOOKING"
    ]
    assert late
    for c in late:
        assert c.recommendation is Recommendation.INDIVIDUAL_REVIEW
        assert "ABSTAINED:INSUFFICIENT_EVIDENCE" in c.reasons
        run = demo.platform.investigation(c.investigation_run_id or "")
        assert run is not None and "SYSTEM_INCIDENT_REFERENCE" in run.missing_evidence


def test_11b_injection_text_is_inert(demo: DemoResult) -> None:
    injected = {a.alert_id for a in demo.dataset.bundle.alerts if a.explanation == INJECTION_TEXT}
    for c in demo.platform.cases():
        if set(c.alert_ids) & injected:
            assert c.conclusion == "PRICE_CORRECTION"  # decided on evidence, same as its peers


def test_12_agent_and_model_failure_fall_back_safely(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset
) -> None:
    def down(_: ModelRequest) -> str:
        raise ModelError("model endpoint unreachable")

    llm_cfg = cfg.with_value(True, "agents", "enabled")
    failing = Platform(tmp_path / "fail.db", llm_cfg)
    failing.runtime.gateway = wrap_gateway(ScriptedGateway(down), llm_cfg, failing.store)
    failing.seed_policy(ruleset_path())
    failing.ingest(dataset.bundle, ADMIN)
    report = failing.run_pipeline(dataset.end, SYSTEM)
    reference = Platform(tmp_path / "ref.db", cfg)
    reference.seed_policy(ruleset_path())
    reference.ingest(dataset.bundle, ADMIN)
    expected = reference.run_pipeline(dataset.end, SYSTEM)
    assert report.agent_failures == 0 and report.cases == expected.cases
    decided = {c.case_id: (c.recommendation, c.conclusion) for c in failing.cases()}
    assert decided == {c.case_id: (c.recommendation, c.conclusion) for c in reference.cases()}
    assert all(
        failing.investigation(c.investigation_run_id or "").fallback_used  # type: ignore[union-attr]
        for c in failing.cases()
    )


def test_12b_crashing_agent_routes_cases_to_individual_review(
    tmp_path: Path, cfg: Config, dataset: SyntheticDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Platform(tmp_path / "crash.db", cfg)
    platform.seed_policy(ruleset_path())
    platform.ingest(dataset.bundle, ADMIN)

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("runtime crashed")

    monkeypatch.setattr(platform, "investigate", broken)
    report = platform.run_pipeline(dataset.end, SYSTEM)
    assert report.agent_failures == report.cases > 0 and report.proposed_bulk == 0
    assert all(any(r.startswith("AGENT_UNAVAILABLE") for r in c.reasons) for c in platform.cases())


def test_evaluation_beats_rule_only_and_score_only(demo: DemoResult) -> None:
    ev = demo.evaluation
    assert float(ev.detection["recall_agentic_system"]) > float(
        ev.detection["recall_production_rules"]
    )
    assert ev.treatment["false_bulk"] == "0" and ev.treatment["bulk_precision_benign"] == "1.0000"
    assert float(ev.treatment["escalation_precision"]) >= 0.9
    assert float(ev.detection["recall_agentic_system"]) > float(
        ev.score_only_baseline["recall_at_k"]
    )
    json.dumps(ev.model_dump(mode="json"))
