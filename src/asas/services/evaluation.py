"""Evaluation harness: score the platform against labelled truth.

Measures:
- linking precision and recall (pairwise)
- detection recall of risky episodes by production rules alone vs the agentic system
  (investigation, challenger, released candidates)
- bulk proposal precision (the false-suppression risk)
- escalation precision and recall
- abstention
- three rankings of the same review window at k = number of risky episodes:
  - the attention score (rank-by-score, the ML-ensemble style)
  - raw verified outlyingness (what an unsupervised outlier model gives)
  - residual outlyingness: deviations no verified benign explanation accounts for
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import combinations

from pydantic import BaseModel, ConfigDict

from asas.data.synthetic import SyntheticTruth
from asas.domain.models import (
    Case,
    Category,
    ChallengeFinding,
    FindingKind,
    HypothesisClass,
    Recommendation,
    RuleSpec,
)
from asas.engine.deviation import residual
from asas.engine.hypotheses import Assessment
from asas.engine.rules import evaluate_ruleset
from asas.engine.snapshot import Snapshot

_Q = Decimal("0.0001")


def _ratio(num: int, den: int) -> str:
    return str((Decimal(num) / Decimal(den)).quantize(_Q)) if den else "n/a"


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    as_of: datetime
    linking: dict[str, str]
    detection: dict[str, str]
    treatment: dict[str, str]
    score_only_baseline: dict[str, str]
    anomaly_ranking: dict[str, str]
    discovery: dict[str, str]


def _pairs(groups: Iterable[Iterable[str]], universe: set[str]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for g in groups:
        members = sorted(t for t in g if t in universe)
        out.update(combinations(members, 2))
    return out


def linking_scores(snap: Snapshot, truth: SyntheticTruth) -> dict[str, str]:
    universe = set(snap.events_by_trade)
    predicted = _pairs((e.trade_ids for e in snap.episodes), universe)
    actual = _pairs(truth.true_groups().values(), universe)
    tp = len(predicted & actual)
    return {
        "predicted_pairs": str(len(predicted)),
        "true_pairs": str(len(actual)),
        "precision": _ratio(tp, len(predicted)),
        "recall": _ratio(tp, len(actual)),
        "unresolved_pairs": str(len(snap.linking.unresolved)),
    }


def evaluate(
    snap: Snapshot,
    truth: SyntheticTruth,
    cases: list[Case],
    assessments: Mapping[str, Assessment],
    findings: Iterable[ChallengeFinding],
    released_rules: Sequence[RuleSpec],
    linking_before: dict[str, str],
) -> EvaluationReport:
    def risky(episode_id: str) -> bool:
        return any(truth.is_risky(t) for t in snap.episodes_by_id[episode_id].trade_ids)

    review_start = snap.as_of - timedelta(days=snap.cfg.integer("pipeline", "review_window_days"))
    all_eps = [e.episode_id for e in snap.episodes if e.end >= review_start]
    risky_eps = [e for e in all_eps if risky(e)]
    sampling = set(snap.cfg.strings("challenger", "sampling_rules"))

    def production_detects(eid: str) -> bool:
        return any(r not in sampling for r in snap.episode_rules(eid))

    escalated = {
        c.episode_id for c in cases if c.recommendation is Recommendation.ESCALATION_RECOMMENDED
    }
    blind = {
        f.episode_id
        for f in findings
        if f.kind is FindingKind.BLIND_SPOT and f.status == "CONFIRMED"
    }
    candidate_detects = {
        eid for eid in all_eps if evaluate_ruleset(released_rules, snap.signal_set.signals[eid])
    }
    flagged = {
        c.episode_id
        for c in cases
        if c.classification is not None
        and c.classification.category in (Category.TYPED_ANOMALY, Category.UNEXPLAINED_DEVIATION)
    }
    agentic = escalated | flagged | blind | candidate_detects
    detection = {
        "risky_episodes_in_window": str(len(risky_eps)),
        "recall_production_rules": _ratio(
            sum(production_detects(e) for e in risky_eps), len(risky_eps)
        ),
        "recall_agentic_system": _ratio(
            sum(production_detects(e) or e in agentic for e in risky_eps), len(risky_eps)
        ),
        "risky_found_only_by_agentic_layer": str(
            sum((e in agentic) and not production_detects(e) for e in risky_eps)
        ),
        "released_rule_detections": str(len(candidate_detects)),
        "risky_found_only_by_deviation_analysis": str(
            sum(
                1
                for e in risky_eps
                if (a := assessments.get(e)) is not None
                and a.adjudication.conclusion == "VERIFIED_PEER_DEVIATION"
            )
        ),
    }
    bulk = [c for c in cases if c.recommendation is Recommendation.PROPOSED_BULK]
    esc = [c for c in cases if c.recommendation is Recommendation.ESCALATION_RECOMMENDED]
    risky_cases = [c for c in cases if risky(c.episode_id)]
    abstained = [c for c in cases if any(r.startswith("ABSTAINED") for r in c.reasons)]
    treatment = {
        "cases": str(len(cases)),
        "proposed_bulk": str(len(bulk)),
        "bulk_precision_benign": _ratio(sum(not risky(c.episode_id) for c in bulk), len(bulk)),
        "false_bulk": str(sum(risky(c.episode_id) for c in bulk)),
        "escalations": str(len(esc)),
        "escalation_precision": _ratio(sum(risky(c.episode_id) for c in esc), len(esc)),
        "escalation_recall_on_alerted_risky": _ratio(
            sum(c.recommendation is Recommendation.ESCALATION_RECOMMENDED for c in risky_cases),
            len(risky_cases),
        ),
        "abstention_rate": _ratio(len(abstained), len(cases)),
        "volume_reduction_bulk_share": _ratio(len(bulk), len(cases)),
    }
    ranked = sorted(all_eps, key=lambda e: (-snap.scores[e].total, e))
    top_k = ranked[: len(risky_eps)]
    score_only = {
        "k (= risky episodes)": str(len(top_k)),
        "precision_at_k": _ratio(sum(risky(e) for e in top_k), len(top_k)),
        "recall_at_k": _ratio(sum(risky(e) for e in top_k), len(risky_eps)),
        "explains_why": "no - a rank without verified hypotheses",
        "proposes_rule_changes": "no",
    }
    anomaly_ranking = _rankings(snap, assessments, all_eps, risky_eps, risky)
    anomalous_assessed = [
        e
        for e in all_eps
        if (a := assessments.get(e)) and a.adjudication.klass is HypothesisClass.ANOMALOUS
    ]
    discovery = {
        "assessed_anomalous_episodes": str(len(anomalous_assessed)),
        "assessment_precision": _ratio(
            sum(risky(e) for e in anomalous_assessed), len(anomalous_assessed)
        ),
        "assessment_recall": _ratio(sum(risky(e) for e in anomalous_assessed), len(risky_eps)),
    }
    return EvaluationReport(
        as_of=snap.as_of,
        linking={
            **{f"before_{k}": v for k, v in linking_before.items()},
            **{f"after_{k}": v for k, v in linking_scores(snap, truth).items()},
        },
        detection=detection,
        treatment=treatment,
        score_only_baseline=score_only,
        anomaly_ranking=anomaly_ranking,
        discovery=discovery,
    )


def _rankings(
    snap: Snapshot,
    assessments: Mapping[str, Assessment],
    episodes: Sequence[str],
    risky_eps: Sequence[str],
    risky: Callable[[str], bool],
) -> dict[str, str]:
    """Precision@k of four queues over the same window (ties broken by attention, then id):
    attention score, raw outlyingness, residual (unexplained) outlyingness, and the full
    verified assessment (typed anomalies and unexplained deviations first)."""
    k = len(risky_eps)
    raw = {e: snap.deviations[e].outlyingness for e in episodes}
    res: dict[str, Decimal] = {}
    for e in episodes:
        a = assessments.get(e)
        unexplained = a.adjudication.unexplained if a is not None else ()
        res[e] = residual(snap.deviations[e], unexplained, snap.cfg)[1]

    def at_k(key: Callable[[str], tuple[Decimal, ...]]) -> str:
        top = sorted(episodes, key=lambda e: (*key(e), e))[:k]
        return _ratio(sum(risky(e) for e in top), len(top))

    def anomalous(e: str) -> bool:
        a = assessments.get(e)
        return a is not None and a.adjudication.klass is HypothesisClass.ANOMALOUS

    flagged = [e for e in episodes if res[e] > 0]
    return {
        "k (= risky episodes)": str(k),
        "precision_at_k_attention_score": at_k(lambda e: (-snap.scores[e].total,)),
        "precision_at_k_raw_outlyingness": at_k(lambda e: (-raw[e], -snap.scores[e].total)),
        "precision_at_k_residual_outlyingness": at_k(lambda e: (-res[e], -snap.scores[e].total)),
        "precision_at_k_verified_assessment": at_k(
            lambda e: (
                Decimal(0) if anomalous(e) else Decimal(1),
                -res[e],
                -snap.scores[e].total,
            )
        ),
        "raw_outlying_episodes": str(sum(1 for e in episodes if raw[e] > 0)),
        "raw_outlying_precision": _ratio(
            sum(risky(e) for e in episodes if raw[e] > 0), sum(1 for e in episodes if raw[e] > 0)
        ),
        "unexplained_episodes": str(len(flagged)),
        "unexplained_precision": _ratio(sum(risky(e) for e in flagged), len(flagged)),
    }


def render_markdown(report: EvaluationReport) -> str:
    def table(title: str, data: dict[str, str]) -> str:
        rows = "\n".join(f"| {k} | {v} |" for k, v in data.items())
        return f"### {title}\n\n| metric | value |\n|---|---|\n{rows}\n"

    return "\n".join(
        [
            f"# ASAS evaluation (as of {report.as_of.isoformat()})\n",
            table("Episode linking (pairwise, vs ground truth)", report.linking),
            table("Detection of risky episodes (review window)", report.detection),
            table("Case treatment", report.treatment),
            table("Score-only baseline (ML-ensemble style ranking)", report.score_only_baseline),
            table("Anomaly ranking: raw vs explained-away outlyingness", report.anomaly_ranking),
            table("Deterministic assessment", report.discovery),
        ]
    )
