"""End-to-end demo flow over realistic synthetic data (also used by the e2e tests).

1. generate + ingest SCP/CAL-shaped data        6. challenge existing detections
2. seed the production policy bundle            7. discover patterns, propose candidates
3. pipeline: link, score, investigate, decide   8. replay -> counterexamples -> shadow
4. analyst confirms agent-verified links        9. analyst submits, approver approves (four-eyes)
5. pipeline again on the corrected episodes    10. pipeline under the new bundle + evaluation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from asas.agents.challenger import ChallengeReport
from asas.agents.discovery import DiscoveryReport
from asas.agents.gateway import ModelGateway
from asas.core.config import Config
from asas.core.logging import get_logger, log_event
from asas.core.security import SYSTEM, Principal, Role
from asas.data.synthetic import GeneratorSpec, SyntheticDataset, generate
from asas.domain.models import CandidateState, LinkStatus, PolicyBundle
from asas.services.evaluation import EvaluationReport, evaluate, linking_scores
from asas.services.platform import PipelineReport, Platform

ADMIN = Principal(user_id="ops.admin", roles=frozenset({Role.ADMIN}))
ANALYST = Principal(user_id="analyst.jane", roles=frozenset({Role.INVESTIGATOR}))
APPROVER = Principal(user_id="approver.omar", roles=frozenset({Role.APPROVER}))
_log = get_logger("demo")


@dataclass
class DemoResult:
    platform: Platform
    dataset: SyntheticDataset
    first_run: PipelineReport
    confirmed_links: list[str]
    second_run: PipelineReport
    challenge: ChallengeReport
    discovery: DiscoveryReport
    candidate_states: dict[str, CandidateState]
    released: list[PolicyBundle]
    final_run: PipelineReport
    evaluation: EvaluationReport
    notes: list[str] = field(default_factory=list)


def ruleset_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "rulesets" / "production-v1.json"


def run_demo(
    db_path: str | Path,
    cfg: Config,
    spec: GeneratorSpec | None = None,
    gateway: ModelGateway | None = None,
) -> DemoResult:
    dataset = generate(spec or GeneratorSpec())
    platform = Platform(db_path, cfg, gateway)
    platform.seed_policy(ruleset_path())
    platform.ingest(dataset.bundle, ADMIN)
    as_of = dataset.end
    linking_before = linking_scores(platform.snapshot(as_of), dataset.truth)

    first = platform.run_pipeline(as_of, SYSTEM)
    platform.resolve_links(as_of, SYSTEM)
    confirmed = []
    for proposal in platform.link_proposals():
        if proposal.status is LinkStatus.VERIFIED:
            platform.confirm_link(proposal.proposal_id, ANALYST)
            confirmed.append(proposal.proposal_id)
    second = platform.run_pipeline(as_of, SYSTEM)

    challenge = platform.challenge(as_of, ANALYST)
    discovery = platform.discover(as_of, ANALYST)
    states: dict[str, CandidateState] = {}
    released: list[PolicyBundle] = []
    for candidate in discovery.candidates:
        if platform.governance.state(candidate.candidate_id) is not CandidateState.DRAFT:
            states[candidate.candidate_id] = platform.governance.state(candidate.candidate_id)
            continue
        state = platform.evaluate_candidate(candidate.candidate_id, as_of, ANALYST)
        if state is CandidateState.SHADOW_PASSED:
            platform.submit(candidate.candidate_id, ANALYST, "evidence chain complete")
            released.append(
                platform.approve(candidate.candidate_id, APPROVER, "approved after review")
            )
        states[candidate.candidate_id] = platform.governance.state(candidate.candidate_id)
        log_event(
            _log,
            "demo.candidate",
            candidate=candidate.candidate_id,
            state=states[candidate.candidate_id].value,
        )

    final = platform.run_pipeline(as_of, SYSTEM)
    snap = platform.snapshot(as_of)
    released_rules = [
        c.rule
        for b in released
        if b.source_candidate
        for c in [platform.governance.candidate(b.source_candidate)]
        if c.rule
    ]
    evaluation = evaluate(
        snap,
        dataset.truth,
        platform.cases(),
        platform.assessments(as_of),
        challenge.findings,
        released_rules,
        linking_before,
    )
    platform.store.put_artifact("evaluation", "latest", final.run_key, evaluation)
    return DemoResult(
        platform,
        dataset,
        first,
        confirmed,
        second,
        challenge,
        discovery,
        states,
        released,
        final,
        evaluation,
    )
