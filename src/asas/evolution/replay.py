"""Historical replay: current production rules vs a candidate, point-in-time.

History points are episodes evaluated as of `episode.end + evaluation delay` (never later
than the snapshot), so a replayed rule sees exactly what production would have seen.
Labels are curated outcomes only; RAW sign-offs are never ground truth.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from asas.core.config import Config
from asas.core.ids import content_hash, stable_id
from asas.domain.models import (
    BulkScope,
    EpisodeSignals,
    HypothesisClass,
    PolicyBundle,
    RuleSpec,
)
from asas.engine.hypotheses import Assessment
from asas.engine.rules import evaluate_rule
from asas.engine.snapshot import Snapshot

_Q = Decimal("0.0001")


class HistoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    episode_id: str
    eval_time: datetime
    desk: str
    label: str | None
    signals: EpisodeSignals
    assessed_class: HypothesisClass | None
    assessed_type: str | None


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fires: int
    labelled: int
    tp: int
    fp: int
    fn: int
    precision: Decimal | None
    recall: Decimal | None


class ReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_id: str
    subject: str
    window_start: datetime
    window_end: datetime
    episodes: int
    current: Metrics
    candidate: Metrics
    combined: Metrics
    added: int
    added_positive: int
    lost: int
    lost_positive: int
    volume_delta_pct: Decimal | None
    sample_added: tuple[str, ...]
    sample_false_positive: tuple[str, ...]
    policy_bundle_id: str
    history_hash: str


class BulkReplayReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_id: str
    subject: str
    window_start: datetime
    window_end: datetime
    episodes: int
    would_be_bulk: int
    labelled_cleared: int
    labelled_escalated: int
    unlabelled: int
    sample_escalated: tuple[str, ...]
    history_hash: str


def eval_times(snap: Snapshot, cfg: Config) -> dict[str, datetime]:
    delay = timedelta(hours=cfg.integer("replay", "evaluation_delay_hours"))
    return {e.episode_id: min(e.end + delay, snap.as_of) for e in snap.episodes}


def build_history(
    snap: Snapshot,
    assessments: Mapping[str, Assessment],
    start: datetime,
    end: datetime,
) -> list[HistoryPoint]:
    points = []
    for ep in snap.episodes:
        s = snap.signal_set.signals[ep.episode_id]
        if not (start <= s.eval_time < end):
            continue
        a = assessments.get(ep.episode_id)
        points.append(
            HistoryPoint(
                episode_id=ep.episode_id,
                eval_time=s.eval_time,
                desk=ep.desk,
                label=snap.curated_label(ep.episode_id),
                signals=s,
                assessed_class=a.adjudication.klass if a else None,
                assessed_type=a.adjudication.conclusion if a else None,
            )
        )
    return sorted(points, key=lambda p: (p.eval_time, p.episode_id))


def metrics(points: Sequence[HistoryPoint], fires: Callable[[HistoryPoint], bool]) -> Metrics:
    fired = [p for p in points if fires(p)]
    labelled = [p for p in points if p.label]
    tp = sum(1 for p in fired if p.label == "ESCALATED")
    fp = sum(1 for p in fired if p.label == "CLEARED")
    fn = sum(1 for p in labelled if p.label == "ESCALATED" and not fires(p))
    precision = (Decimal(tp) / Decimal(tp + fp)).quantize(_Q) if tp + fp else None
    recall = (Decimal(tp) / Decimal(tp + fn)).quantize(_Q) if tp + fn else None
    return Metrics(
        fires=len(fired),
        labelled=len(labelled),
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
    )


def _fires(rules: Sequence[RuleSpec]) -> Callable[[HistoryPoint], bool]:
    return lambda p: any(evaluate_rule(r, p.signals) is not None for r in rules)


def replay_detection(
    rule: RuleSpec,
    bundle: PolicyBundle,
    points: Sequence[HistoryPoint],
    start: datetime,
    end: datetime,
    sample: int,
) -> ReplayReport:
    current_rules = list(bundle.ruleset.rules)
    candidate_rules = [r for r in current_rules if r.rule_id != rule.rule_id] + [rule]
    cur, cand = _fires(current_rules), _fires([rule])
    combined = _fires(candidate_rules)
    added = [p for p in points if combined(p) and not cur(p)]
    lost = [p for p in points if cur(p) and not combined(p)]
    before = sum(1 for p in points if cur(p))
    after = sum(1 for p in points if combined(p))
    history_hash = content_hash([(p.episode_id, p.eval_time, p.label) for p in points])[:16]
    return ReplayReport(
        report_id=stable_id("RPL", rule.model_dump_json(), history_hash, bundle.bundle_id),
        subject=rule.subrule_id,
        window_start=start,
        window_end=end,
        episodes=len(points),
        current=metrics(points, cur),
        candidate=metrics(points, cand),
        combined=metrics(points, combined),
        added=len(added),
        added_positive=sum(1 for p in added if p.label == "ESCALATED"),
        lost=len(lost),
        lost_positive=sum(1 for p in lost if p.label == "ESCALATED"),
        volume_delta_pct=((Decimal(after - before) / Decimal(before)) * 100).quantize(_Q)
        if before
        else None,
        sample_added=tuple(p.episode_id for p in added[:sample]),
        sample_false_positive=tuple(
            p.episode_id for p in points if cand(p) and p.label == "CLEARED"
        )[:sample],
        policy_bundle_id=bundle.bundle_id,
        history_hash=history_hash,
    )


def bulk_members(scope: BulkScope, points: Sequence[HistoryPoint]) -> list[HistoryPoint]:
    return [
        p
        for p in points
        if p.assessed_class is HypothesisClass.BENIGN
        and p.assessed_type == scope.hypothesis_type
        and scope.desk in ("*", p.desk)
    ]


def replay_bulk(
    scope: BulkScope, points: Sequence[HistoryPoint], start: datetime, end: datetime, sample: int
) -> BulkReplayReport:
    members = bulk_members(scope, points)
    history_hash = content_hash([(p.episode_id, p.eval_time, p.label) for p in points])[:16]
    escalated = [p for p in members if p.label == "ESCALATED"]
    return BulkReplayReport(
        report_id=stable_id("RPB", scope.model_dump_json(), history_hash),
        subject=f"{scope.hypothesis_type}@{scope.desk}",
        window_start=start,
        window_end=end,
        episodes=len(points),
        would_be_bulk=len(members),
        labelled_cleared=sum(1 for p in members if p.label == "CLEARED"),
        labelled_escalated=len(escalated),
        unlabelled=sum(1 for p in members if p.label is None),
        sample_escalated=tuple(p.episode_id for p in escalated[:sample]),
        history_hash=history_hash,
    )
