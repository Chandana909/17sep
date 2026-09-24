"""Shadow execution: run a candidate beside production on the most recent, live-like window
without changing any case, alert or queue. Only would-have-fired decisions are recorded."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from asas.core.config import Config
from asas.core.ids import stable_id
from asas.domain.models import BulkScope, HypothesisClass, PolicyBundle, RuleSpec
from asas.engine.rules import evaluate_rule
from asas.evolution.replay import HistoryPoint, bulk_members


class ShadowDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    episode_id: str
    eval_time: datetime
    production_fired: bool
    candidate_fired: bool


class ShadowReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_id: str
    subject: str
    window_start: datetime
    window_end: datetime
    episodes: int
    production_fires: int
    candidate_fires: int
    overlap: int
    candidate_only: int
    candidate_only_assessed_anomalous: int
    max_new_per_day: int
    passed: bool
    failures: tuple[str, ...]
    decisions: tuple[ShadowDecision, ...]


def shadow_detection(
    rule: RuleSpec,
    bundle: PolicyBundle,
    points: Sequence[HistoryPoint],
    start: datetime,
    end: datetime,
    cfg: Config,
) -> ShadowReport:
    decisions = []
    for p in points:
        prod = any(evaluate_rule(r, p.signals) is not None for r in bundle.ruleset.rules)
        cand = evaluate_rule(rule, p.signals) is not None
        decisions.append(
            ShadowDecision(
                episode_id=p.episode_id,
                eval_time=p.eval_time,
                production_fired=prod,
                candidate_fired=cand,
            )
        )
    by_id = {p.episode_id: p for p in points}
    only = [d for d in decisions if d.candidate_fired and not d.production_fired]
    per_day = Counter(d.eval_time.date() for d in only)
    anomalous = sum(
        1 for d in only if by_id[d.episode_id].assessed_class is HypothesisClass.ANOMALOUS
    )
    failures = []
    max_new = max(per_day.values(), default=0)
    if max_new > cfg.integer("shadow", "max_new_alerts_per_day"):
        failures.append("candidate adds too many alerts per day")
    if only and Decimal(anomalous) / Decimal(len(only)) < cfg.decimal(
        "shadow", "min_assessed_precision"
    ):
        failures.append("new alerts are rarely corroborated by investigation")
    if not any(d.candidate_fired for d in decisions):
        failures.append("candidate never fired in shadow; nothing to learn")
    return ShadowReport(
        report_id=stable_id("SHD", rule.model_dump_json(), start.isoformat(), end.isoformat()),
        subject=rule.subrule_id,
        window_start=start,
        window_end=end,
        episodes=len(points),
        production_fires=sum(d.production_fired for d in decisions),
        candidate_fires=sum(d.candidate_fired for d in decisions),
        overlap=sum(d.candidate_fired and d.production_fired for d in decisions),
        candidate_only=len(only),
        candidate_only_assessed_anomalous=anomalous,
        max_new_per_day=max_new,
        passed=not failures,
        failures=tuple(failures),
        decisions=tuple(decisions),
    )


def shadow_bulk(
    scope: BulkScope, points: Sequence[HistoryPoint], start: datetime, end: datetime, cfg: Config
) -> ShadowReport:
    members = {p.episode_id for p in bulk_members(scope, points)}
    decisions = tuple(
        ShadowDecision(
            episode_id=p.episode_id,
            eval_time=p.eval_time,
            production_fired=False,
            candidate_fired=p.episode_id in members,
        )
        for p in points
    )
    escalated = [p for p in points if p.episode_id in members and p.label == "ESCALATED"]
    failures = ["a live escalation would have been proposed for bulk"] if escalated else []
    if not members:
        failures.append("policy never applied in shadow; nothing to learn")
    return ShadowReport(
        report_id=stable_id("SHB", scope.model_dump_json(), start.isoformat(), end.isoformat()),
        subject=f"{scope.hypothesis_type}@{scope.desk}",
        window_start=start,
        window_end=end,
        episodes=len(points),
        production_fires=0,
        candidate_fires=len(members),
        overlap=0,
        candidate_only=len(members),
        candidate_only_assessed_anomalous=0,
        max_new_per_day=0,
        passed=not failures,
        failures=tuple(failures),
        decisions=decisions,
    )
