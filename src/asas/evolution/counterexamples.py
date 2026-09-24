"""Counterexample engine: attack a candidate before anyone trusts it.

Attacks on a detection rule:
  FALSE_POSITIVE        fires on curated-cleared history
  FALSE_NEGATIVE        misses curated escalations of the rule's own target typology
  BOUNDARY              history within a configured band of a numeric threshold
  MUTATION_SURVIVOR     a true positive still fires after one condition is violated minimally
                        (the condition is dead weight)
  NEAR_MISS             cleared activity that satisfies all but one condition
  TEMPORAL_INSTABILITY  labelled precision differs across the two halves of the window
Attacks on a bulk-policy proposal:
  FALSE_BULK            a curated escalation the policy would have routed to bulk review
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from asas.core.config import Config
from asas.core.ids import stable_id
from asas.domain.models import BulkScope, Condition, EpisodeSignals, Op, RuleSpec
from asas.engine.rules import evaluate_rule
from asas.engine.signals import signal_kind
from asas.evolution.replay import HistoryPoint, bulk_members, metrics


class Counterexample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: str
    episode_id: str | None
    detail: dict[str, str]


class CounterexampleReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    report_id: str
    subject: str
    passed: bool
    failures: tuple[str, ...]
    counts: dict[str, int]
    counterexamples: tuple[Counterexample, ...]
    temporal: dict[str, str]


def _violate(c: Condition, rule: RuleSpec, signals: EpisodeSignals, eps: Decimal) -> EpisodeSignals:
    """Smallest change to `signals` that makes condition `c` false."""
    raw = rule.parameters[c.param] if c.param else (c.value or "")
    kind = signal_kind(c.signal)
    numeric, flags, labels = dict(signals.numeric), dict(signals.flags), dict(signals.labels)
    if kind == "numeric":
        t = Decimal(raw)
        step = max(abs(t) * eps, eps)
        numeric[c.signal] = {
            Op.GE: t - step,
            Op.GT: t,
            Op.LE: t + step,
            Op.LT: t,
            Op.EQ: t + step,
            Op.NE: t,
        }.get(c.op, t)
    elif kind == "flag":
        wanted = raw == "true"
        flags[c.signal] = (not wanted) if c.op is Op.EQ else wanted
    else:
        labels[c.signal] = "__MUTATED__" if c.op in (Op.EQ, Op.IN) else raw
    return signals.model_copy(update={"numeric": numeric, "flags": flags, "labels": labels})


def attack_detection(
    rule: RuleSpec, points: Sequence[HistoryPoint], cfg: Config
) -> CounterexampleReport:
    eps = cfg.decimal("counterexamples", "perturbation_pct") / Decimal(100)
    band = cfg.decimal("counterexamples", "boundary_pct") / Decimal(100)
    limit = cfg.integer("counterexamples", "max_listed")
    fires = {p.episode_id: evaluate_rule(rule, p.signals) is not None for p in points}
    found: list[Counterexample] = []
    counts: Counter[str] = Counter()

    def add(kind: str, episode_id: str | None, **detail: str) -> None:
        counts[kind] += 1
        if counts[kind] <= limit:
            found.append(Counterexample(kind=kind, episode_id=episode_id, detail=detail))

    positives = [p for p in points if fires[p.episode_id] and p.label == "ESCALATED"]
    target = Counter(p.assessed_type for p in positives if p.assessed_type).most_common(1)
    target_type = target[0][0] if target else None
    for p in points:
        if fires[p.episode_id] and p.label == "CLEARED":
            add("FALSE_POSITIVE", p.episode_id, desk=p.desk)
        if (
            not fires[p.episode_id]
            and p.label == "ESCALATED"
            and target_type
            and p.assessed_type == target_type
        ):
            add("FALSE_NEGATIVE", p.episode_id, typology=target_type)
        for c in rule.conditions:
            if signal_kind(c.signal) != "numeric":
                continue
            threshold = Decimal(rule.parameters[c.param] if c.param else (c.value or "0"))
            value = p.signals.numeric.get(c.signal)
            if value is not None and threshold and abs(value - threshold) <= abs(threshold) * band:
                add(
                    "BOUNDARY",
                    p.episode_id,
                    signal=c.signal,
                    fired=str(fires[p.episode_id]).lower(),
                    label=p.label or "unlabelled",
                )
        if p.label == "CLEARED" and not fires[p.episode_id]:
            failing = [
                c
                for c in rule.conditions
                if evaluate_rule(rule.model_copy(update={"conditions": (c,)}), p.signals) is None
            ]
            if len(failing) == 1:
                add("NEAR_MISS", p.episode_id, only_failing=failing[0].signal)
    for p in positives:
        for c in rule.conditions:
            if evaluate_rule(rule, _violate(c, rule, p.signals, eps)) is not None:
                add("MUTATION_SURVIVOR", p.episode_id, condition=c.signal)

    labelled = [p for p in points if p.label]
    half = len(points) // 2
    first, second = points[:half], points[half:]
    m1 = metrics(first, lambda p: fires[p.episode_id])
    m2 = metrics(second, lambda p: fires[p.episode_id])
    overall = metrics(points, lambda p: fires[p.episode_id])
    temporal = {
        "first_precision": str(m1.precision),
        "second_precision": str(m2.precision),
        "first_labelled_fires": str(m1.tp + m1.fp),
        "second_labelled_fires": str(m2.tp + m2.fp),
    }
    min_n = cfg.integer("counterexamples", "min_labelled_per_half")
    if (
        m1.precision is not None
        and m2.precision is not None
        and m1.tp + m1.fp >= min_n
        and m2.tp + m2.fp >= min_n
        and abs(m1.precision - m2.precision) > cfg.decimal("counterexamples", "stability_tolerance")
    ):
        add("TEMPORAL_INSTABILITY", None, first=str(m1.precision), second=str(m2.precision))

    failures = []
    if overall.tp < cfg.integer("counterexamples", "min_true_positives"):
        failures.append("too few labelled true positives to trust the rule")
    if overall.precision is None or overall.precision < cfg.decimal(
        "counterexamples", "min_precision"
    ):
        failures.append("labelled precision below the floor")
    if counts["FALSE_POSITIVE"] > cfg.integer("counterexamples", "max_false_positives"):
        failures.append("too many false positives on curated history")
    if counts["MUTATION_SURVIVOR"]:
        failures.append("a condition has no effect on true positives")
    if counts["TEMPORAL_INSTABILITY"]:
        failures.append("precision unstable across time")
    fn = counts["FALSE_NEGATIVE"]
    if (
        target_type
        and overall.tp
        and Decimal(overall.tp) / Decimal(overall.tp + fn)
        < cfg.decimal("counterexamples", "min_target_recall")
    ):
        failures.append("misses too much of its own target typology")
    del labelled
    return CounterexampleReport(
        report_id=stable_id("CEX", rule.model_dump_json(), str(len(points))),
        subject=rule.subrule_id,
        passed=not failures,
        failures=tuple(failures),
        counts=dict(counts),
        counterexamples=tuple(found),
        temporal=temporal,
    )


def attack_bulk(
    scope: BulkScope, points: Sequence[HistoryPoint], cfg: Config
) -> CounterexampleReport:
    members = bulk_members(scope, points)
    limit = cfg.integer("counterexamples", "max_listed")
    escalated = [p for p in members if p.label == "ESCALATED"]
    cleared = [p for p in members if p.label == "CLEARED"]
    found = [
        Counterexample(kind="FALSE_BULK", episode_id=p.episode_id, detail={"desk": p.desk})
        for p in escalated[:limit]
    ]
    failures = []
    if escalated:
        failures.append("a curated escalation would have been proposed for bulk review")
    if len(cleared) < cfg.integer("counterexamples", "min_bulk_cleared"):
        failures.append("not enough curated clean history for this scope")
    return CounterexampleReport(
        report_id=stable_id("CEB", scope.model_dump_json(), str(len(points))),
        subject=f"{scope.hypothesis_type}@{scope.desk}",
        passed=not failures,
        failures=tuple(failures),
        counts={"FALSE_BULK": len(escalated), "CLEARED_SUPPORT": len(cleared)},
        counterexamples=tuple(found),
        temporal={},
    )
