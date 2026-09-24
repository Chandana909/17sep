"""Domain records. Decision records carry no workflow or person fields: those live only in
annex records that decision code never receives (structural enforcement)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

FROZEN = ConfigDict(frozen=True, extra="forbid")


class Record(BaseModel):
    model_config = FROZEN


# ---------------------------------------------------------------- source data


class EventType(StrEnum):
    NEW = "NEW"
    AMEND = "AMEND"
    CANCEL = "CANCEL"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OutcomeLabel(StrEnum):
    CLEARED = "CLEARED"
    ESCALATED = "ESCALATED"


class LabelQuality(StrEnum):
    RAW = "RAW"
    CURATED = "CURATED"


class RfiAction(StrEnum):
    OPENED = "OPENED"
    CLOSED = "CLOSED"


class TradeEvent(Record):
    trade_id: str
    version: int
    event_type: EventType
    event_time: datetime
    record_time: datetime
    book: str
    desk: str
    instrument_id: str
    product_type: str
    side: Side | None
    quantity: Decimal | None
    price: Decimal | None
    currency: str
    notional_usd: Decimal | None
    original_trade_id: str | None = None
    alternate_trade_id: str | None = None
    urn_ref: str | None = None
    source: str


class Alert(Record):
    alert_id: str
    rule_id: str
    subrule_id: str
    alert_time: datetime
    record_time: datetime
    trade_id: str
    trade_version: int | None
    book: str
    desk: str
    instrument_id: str
    explanation: str | None  # untrusted free text


class AlertAnnex(Record):
    """Quarantined workflow/outcome and person fields. Reports only, entitlement-gated."""

    alert_id: str
    record_time: datetime
    workflow: dict[str, str]
    persons: dict[str, str]


class TradePerson(Record):
    """Quarantined: who booked a trade. Only the entitled trader-baseline tool reads it."""

    trade_id: str
    record_time: datetime
    trader_id: str


class RfiEvent(Record):
    alert_id: str
    action: RfiAction
    record_time: datetime


class ReviewOutcome(Record):
    outcome_id: str
    alert_id: str
    label: OutcomeLabel
    quality: LabelQuality
    decided_at: datetime
    decided_by: str


# ---------------------------------------------------------------- linking


class LinkKind(StrEnum):
    REBOOK_OF = "REBOOK_OF"
    SHARED_URN = "SHARED_URN"
    ALTERNATE_ID = "ALTERNATE_ID"
    AGENT_LINK = "AGENT_LINK"


class LinkTier(StrEnum):
    STRONG = "S"
    MEDIUM = "M"
    AGENT = "A"


class LinkStatus(StrEnum):
    CANONICAL = "CANONICAL"
    PROPOSED = "PROPOSED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class TradeLink(Record):
    src: str
    dst: str
    kind: LinkKind
    tier: LinkTier
    status: LinkStatus
    provenance: str
    reason: str
    evidence: dict[str, str] = Field(default_factory=dict)


class Episode(Record):
    episode_id: str
    trade_ids: tuple[str, ...]
    alert_ids: tuple[str, ...]
    start: datetime
    end: datetime
    desks: tuple[str, ...]
    books: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    product_types: tuple[str, ...]
    links: tuple[TradeLink, ...]
    rejected_links: tuple[TradeLink, ...]
    quality_flags: tuple[str, ...]
    linking_version: str

    @property
    def desk(self) -> str:
        return self.desks[0] if self.desks else "UNKNOWN"


class UnresolvedPair(Record):
    """A plausible relationship deterministic linking could not prove (agent residue)."""

    pair_id: str
    cancelled_trade: str
    candidate_trade: str
    episode_a: str
    episode_b: str
    features: dict[str, str]


# ---------------------------------------------------------------- signals and scoring


class EpisodeSignals(Record):
    episode_id: str
    eval_time: datetime
    numeric: dict[str, Decimal]
    flags: dict[str, bool]
    labels: dict[str, str]

    def get(self, name: str) -> Decimal | bool | str | None:
        if name in self.numeric:
            return self.numeric[name]
        if name in self.flags:
            return self.flags[name]
        return self.labels.get(name)


class OutlierEvidence(Record):
    signal: str
    value: Decimal
    percentile: Decimal
    robust_z: Decimal | None
    peer_level: str
    peer_n: int


class ScoreComponent(Record):
    name: str
    raw: Decimal
    weight: Decimal
    contribution: Decimal
    detail: str


class EpisodeScore(Record):
    episode_id: str
    total: Decimal
    band: str
    bucket: str
    components: tuple[ScoreComponent, ...]
    outliers: tuple[OutlierEvidence, ...]
    overrides: tuple[str, ...]
    degraded: tuple[str, ...]


# ---------------------------------------------------------------- rules and policy


class Op(StrEnum):
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    EQ = "eq"
    NE = "ne"
    IN = "in"


class Condition(Record):
    signal: str
    op: Op
    value: str | None = None
    param: str | None = None


class RuleSpec(Record):
    rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,40}$")
    subrule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_.]{1,48}$")
    description: str
    severity: str
    conditions: tuple[Condition, ...]
    parameters: dict[str, str] = Field(default_factory=dict)


class RuleSet(Record):
    rules: tuple[RuleSpec, ...]


class Detection(Record):
    rule_id: str
    subrule_id: str
    episode_id: str
    matched: tuple[str, ...]


class BulkScope(Record):
    hypothesis_type: str
    desk: str  # "*" for all desks


class BulkPolicy(Record):
    allowed: tuple[BulkScope, ...]


class PolicyBundle(Record):
    bundle_id: str
    version: int
    parent_id: str | None
    ruleset: RuleSet
    bulk_policy: BulkPolicy
    created_at: datetime
    created_by: str
    source_candidate: str | None
    notes: str


# ---------------------------------------------------------------- investigation


class HypothesisClass(StrEnum):
    BENIGN = "BENIGN"
    ANOMALOUS = "ANOMALOUS"
    CONTEXT = "CONTEXT"


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT = "INSUFFICIENT"


class EvidenceRecord(Record):
    evidence_id: str
    tool: str
    call_id: str
    args: dict[str, str]
    facts: dict[str, str]
    lead_only: bool = False


class Hypothesis(Record):
    hypothesis_id: str
    type: str
    klass: HypothesisClass
    status: HypothesisStatus
    rationale: str
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class LinkProposal(Record):
    proposal_id: str
    trade_a: str
    trade_b: str
    relation: str
    status: LinkStatus
    proposed_by: str
    verification: dict[str, str]
    evidence_ids: tuple[str, ...]
    created_at: datetime


class InvestigationResult(Record):
    run_id: str
    case_id: str
    episode_id: str
    policy: str
    fallback_used: bool
    hypotheses: tuple[Hypothesis, ...]
    evidence: tuple[EvidenceRecord, ...]
    conclusion: str | None
    conclusion_class: HypothesisClass | None
    abstained: bool
    abstain_reason: str | None
    missing_evidence: tuple[str, ...]
    contradictions: tuple[str, ...]
    link_proposals: tuple[LinkProposal, ...]
    explanation: str
    steps: int
    tool_calls: int


# ---------------------------------------------------------------- cases


class Recommendation(StrEnum):
    PROPOSED_BULK = "PROPOSED_BULK"
    INDIVIDUAL_REVIEW = "INDIVIDUAL_REVIEW"
    ESCALATION_RECOMMENDED = "ESCALATION_RECOMMENDED"


class CaseState(StrEnum):
    OPEN = "OPEN"
    INVESTIGATED = "INVESTIGATED"
    DECIDED = "DECIDED"


class Case(Record):
    case_id: str
    episode_id: str
    as_of: datetime
    alert_ids: tuple[str, ...]
    desk: str
    state: CaseState
    recommendation: Recommendation
    reasons: tuple[str, ...]
    score: EpisodeScore
    investigation_run_id: str | None
    conclusion: str | None
    control_sample: bool
    cohort_id: str | None


class Cohort(Record):
    cohort_id: str
    hypothesis_type: str
    desk: str
    case_ids: tuple[str, ...]
    status: str = "PROPOSED_AWAITING_SUPERVISOR_ATTESTATION"


# ---------------------------------------------------------------- challenge and discovery


class FindingKind(StrEnum):
    REDUNDANT_DETECTION = "REDUNDANT_DETECTION"
    ALTERNATIVE_EXPLANATION = "ALTERNATIVE_EXPLANATION"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    BLIND_SPOT = "BLIND_SPOT"


class ChallengeFinding(Record):
    finding_id: str
    kind: FindingKind
    episode_id: str
    rule_ids: tuple[str, ...]
    alert_ids: tuple[str, ...]
    facts: dict[str, str]
    summary: str
    status: str
    run_id: str


class PatternKind(StrEnum):
    UNCAPTURED_RISK = "UNCAPTURED_RISK"
    RECURRING_BENIGN = "RECURRING_BENIGN"


class Pattern(Record):
    pattern_id: str
    kind: PatternKind
    items: tuple[str, ...]
    support: int
    labelled: int
    escalated: int
    cleared: int
    rule_coverage: Decimal
    benign_verified: int
    risk_evidence: int = 0
    sample_episode_ids: tuple[str, ...]
    scope_desk: str


class CandidateKind(StrEnum):
    DETECTION_RULE = "DETECTION_RULE"
    BULK_POLICY = "BULK_POLICY"


class CandidateState(StrEnum):
    DRAFT = "DRAFT"
    REPLAYED = "REPLAYED"
    COUNTEREXAMPLES_PASSED = "COUNTEREXAMPLES_PASSED"
    COUNTEREXAMPLES_FAILED = "COUNTEREXAMPLES_FAILED"
    SHADOW_PASSED = "SHADOW_PASSED"
    SHADOW_FAILED = "SHADOW_FAILED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


class Candidate(Record):
    candidate_id: str
    kind: CandidateKind
    rule: RuleSpec | None
    bulk_scope: BulkScope | None
    pattern_id: str
    rationale: str
    proposed_by: str
    created_at: datetime
    base_bundle_id: str


class CandidateEvent(Record):
    candidate_id: str
    state: CandidateState
    actor: str
    at: datetime
    artifact_hash: str | None
    note: str
