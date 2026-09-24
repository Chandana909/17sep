"""Evaluation harness: score the platform against labelled truth.

Measures linking precision/recall (pairwise), detection recall of risky episodes by production
rules alone vs the agentic system (investigation + challenger + released candidates), bulk
proposal precision (the false-suppression risk), escalation precision/recall, abstention, and
a score-only baseline (rank-by-score, the ML-ensemble style) for comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import combinations

from pydantic import BaseModel, ConfigDict

from asas.data.synthetic import SyntheticTruth
from asas.domain.models import (
    Case,
    ChallengeFinding,
    FindingKind,
    HypothesisClass,
    Recommendation,
    RuleSpec,
)
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
    agentic = escalated | blind | candidate_detects
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
        discovery=discovery,
    )


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
            table("Deterministic assessment", report.discovery),
        ]
    )
