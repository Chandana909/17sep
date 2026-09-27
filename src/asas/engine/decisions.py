"""Deterministic case decisions: the gate between investigation and human review.

A case may be proposed for bulk attestation only when an investigation concluded a BENIGN
hypothesis that deterministic verification SUPPORTED, no verified deviation is left
unexplained, the active bulk policy covers it, no override / high-attention / data-quality /
unconfirmed-link reason exists, and it was not drawn into the control sample. Everything else
goes to individual review. Typed anomalies are recommended for escalation; an unexplained
verified deviation is escalated when its residual outlyingness reaches
`anomaly.residual_escalation_band`, otherwise it is prioritised for individual review.
Nothing is closed by the system.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from asas.core.config import Config
from asas.core.ids import fraction_from_hash, stable_id
from asas.domain.models import (
    BulkPolicy,
    Case,
    CaseState,
    Cohort,
    DeviationProfile,
    Episode,
    EpisodeScore,
    HypothesisClass,
    HypothesisStatus,
    InvestigationResult,
    LinkStatus,
    Recommendation,
)
from asas.engine.classification import classify
from asas.engine.deviation import band_rank
from asas.engine.hypotheses import CATALOG_BY_TYPE

# Every reason prefix a case can carry (business meaning: config/business_context.toml).
REASON_CODES = (
    "AGENT_UNAVAILABLE",
    "ABSTAINED",
    "ANOMALY_SUPPORTED",
    "UNEXPLAINED_DEVIATION",
    "BULK_POLICY_NOT_ELIGIBLE",
    "UNCONFIRMED_AGENT_LINK",
    "HISTORY_ADVERSE",
    "OVERRIDE",
    "HIGH_ATTENTION_SCORE",
    "DEGRADED",
    "DATA_QUALITY",
    "CONTROL_SAMPLE",
    "COHORT_BELOW_MIN_SIZE",
    "DATA_GATE",
    "SAFE_MODE",
)


def case_id_for(episode_id: str) -> str:
    return stable_id("CASE", episode_id)


def policy_allows(policy: BulkPolicy, hypothesis_type: str, desk: str) -> bool:
    return any(
        s.hypothesis_type == hypothesis_type and s.desk in ("*", desk) for s in policy.allowed
    )


def decide_case(
    episode: Episode,
    score: EpisodeScore,
    investigation: InvestigationResult | None,
    investigation_error: str | None,
    policy: BulkPolicy,
    cfg: Config,
    as_of: datetime,
    deviation: DeviationProfile | None = None,
    rules: tuple[str, ...] = (),
    notional_usd: Decimal | None = None,
    run_blocks: Sequence[str] = (),
) -> Case:
    case_id = case_id_for(episode.episode_id)
    reasons: list[str] = []
    recommendation = Recommendation.INDIVIDUAL_REVIEW
    conclusion = investigation.conclusion if investigation else None
    classification = classify(investigation, deviation, rules, notional_usd, cfg)

    if investigation is None:
        reasons.append(f"AGENT_UNAVAILABLE:{investigation_error or 'NOT_RUN'}")
    elif investigation.abstained:
        reasons.append(f"ABSTAINED:{investigation.abstain_reason}")
    elif investigation.conclusion_class is HypothesisClass.ANOMALOUS and (
        conclusion is not None and CATALOG_BY_TYPE[conclusion].residual
    ):
        reasons.append(f"UNEXPLAINED_DEVIATION:{','.join(investigation.unexplained_deviations)}")
        threshold = cfg.string("anomaly", "residual_escalation_band")
        if band_rank(classification.residual_band) >= band_rank(threshold):
            recommendation = Recommendation.ESCALATION_RECOMMENDED
    elif investigation.conclusion_class is HypothesisClass.ANOMALOUS:
        recommendation = Recommendation.ESCALATION_RECOMMENDED
        reasons.append(f"ANOMALY_SUPPORTED:{conclusion}")
    elif conclusion and not policy_allows(policy, conclusion, episode.desk):
        reasons.append(f"BULK_POLICY_NOT_ELIGIBLE:{conclusion}")

    if investigation is not None:
        if any(p.status is LinkStatus.VERIFIED for p in investigation.link_proposals):
            reasons.append("UNCONFIRMED_AGENT_LINK")
        if any(
            h.type == "RECURRING_BENIGN_CONTEXT" and h.status is HypothesisStatus.CONTRADICTED
            for h in investigation.hypotheses
        ):
            reasons.append("HISTORY_ADVERSE")
    reasons.extend(f"OVERRIDE:{o}" for o in score.overrides)
    if score.band == "HIGH":
        reasons.append("HIGH_ATTENTION_SCORE")
    blocking_degraded = set(cfg.strings("decisions", "blocking_degraded"))
    reasons.extend(f"DEGRADED:{d}" for d in score.degraded if d.split(":")[0] in blocking_degraded)
    blocking_quality = set(cfg.strings("decisions", "blocking_quality_flags"))
    reasons.extend(f"DATA_QUALITY:{f}" for f in episode.quality_flags if f in blocking_quality)
    reasons.extend(run_blocks)  # run-level blocks: failed data gates, ops safe mode

    control = False
    benign = investigation is not None and investigation.conclusion_class is HypothesisClass.BENIGN
    if recommendation is not Recommendation.ESCALATION_RECOMMENDED and benign and not reasons:
        rate = cfg.decimal("decisions", "control_sample_rate")
        if fraction_from_hash(cfg.string("decisions", "control_sample_seed"), case_id) < rate:
            control = True
            reasons.append("CONTROL_SAMPLE")
        else:
            recommendation = Recommendation.PROPOSED_BULK

    return Case(
        case_id=case_id,
        episode_id=episode.episode_id,
        as_of=as_of,
        alert_ids=episode.alert_ids,
        desk=episode.desk,
        state=CaseState.INVESTIGATED if investigation else CaseState.OPEN,
        recommendation=recommendation,
        reasons=tuple(dict.fromkeys(reasons)),
        score=score,
        investigation_run_id=investigation.run_id if investigation else None,
        conclusion=conclusion,
        control_sample=control,
        cohort_id=None,
        classification=classification,
        deviation=deviation,
    )


def form_cohorts(cases: Sequence[Case], cfg: Config) -> tuple[list[Case], list[Cohort]]:
    """Group bulk proposals by (verified explanation, desk). Undersized groups and remainders
    fall back to individual review; nothing is dropped."""
    min_size = cfg.integer("decisions", "cohort_min_size")
    max_size = cfg.integer("decisions", "cohort_max_size")
    groups: dict[tuple[str, str], list[Case]] = defaultdict(list)
    final: dict[str, Case] = {}
    for c in cases:
        if c.recommendation is Recommendation.PROPOSED_BULK and c.conclusion:
            groups[(c.conclusion, c.desk)].append(c)
        else:
            final[c.case_id] = c
    cohorts: list[Cohort] = []
    for (hypothesis, desk), members in sorted(groups.items()):
        members.sort(key=lambda c: c.case_id)
        for start in range(0, len(members), max_size):
            chunk = members[start : start + max_size]
            if len(chunk) < min_size:
                for c in chunk:
                    final[c.case_id] = c.model_copy(
                        update={
                            "recommendation": Recommendation.INDIVIDUAL_REVIEW,
                            "reasons": (*c.reasons, "COHORT_BELOW_MIN_SIZE"),
                        }
                    )
                continue
            cohort_id = stable_id("COH", *(c.case_id for c in chunk))
            cohorts.append(
                Cohort(
                    cohort_id=cohort_id,
                    hypothesis_type=hypothesis,
                    desk=desk,
                    case_ids=tuple(c.case_id for c in chunk),
                )
            )
            for c in chunk:
                final[c.case_id] = c.model_copy(update={"cohort_id": cohort_id})
    return [final[k] for k in sorted(final)], cohorts
