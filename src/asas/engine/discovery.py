"""Deterministic pattern mining and constrained rule synthesis.

Episodes become itemsets (event signature, lifecycle flags, desk, magnitude bands). Frequent
itemsets are scored against curated outcomes, deterministic assessments and current rule
coverage:
  UNCAPTURED_RISK   risky evidence concentrated where production rules do not fire
  RECURRING_BENIGN  consistently cleared, verified-benign patterns outside the bulk policy
Risk patterns are synthesized into the smallest rule in the DSL that keeps labelled precision.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from asas.core.config import Config
from asas.core.ids import stable_id
from asas.domain.models import (
    BulkPolicy,
    BulkScope,
    Condition,
    EpisodeSignals,
    HypothesisClass,
    Op,
    Pattern,
    PatternKind,
    RuleSpec,
)
from asas.engine.decisions import policy_allows
from asas.engine.rules import evaluate_rule
from asas.engine.signals import FLAG_SIGNALS


@dataclass(frozen=True)
class EpisodeFacts:
    episode_id: str
    desk: str
    items: frozenset[str]
    label: str | None
    assessed_class: HypothesisClass | None
    assessed_type: str | None
    detected: bool


def episode_items(signals: EpisodeSignals, cfg: Config) -> frozenset[str]:
    items = {f"sig={signals.labels['sequence_signature']}", f"desk={signals.labels['desk']}"}
    items |= {f"flag:{name}" for name in FLAG_SIGNALS if signals.flags.get(name)}
    for name, cuts in cfg.section("discovery", "bands").items():
        value = signals.numeric.get(name)
        if value is None:
            continue
        edges = [Decimal(c) for c in cuts]
        band = sum(1 for c in edges if value >= c)
        items.add(f"band:{name}:{band}")
    return frozenset(items)


def _item_condition(item: str, cfg: Config) -> list[Condition]:
    if item.startswith("sig="):
        return [Condition(signal="sequence_signature", op=Op.EQ, value=item[4:])]
    if item.startswith("desk="):
        return [Condition(signal="desk", op=Op.EQ, value=item[5:])]
    if item.startswith("flag:"):
        return [Condition(signal=item[5:], op=Op.EQ, value="true")]
    _, name, band = item.split(":")
    edges = [str(c) for c in cfg.section("discovery", "bands")[name]]
    idx = int(band)
    conds: list[Condition] = []
    if idx > 0:
        conds.append(Condition(signal=name, op=Op.GE, value=edges[idx - 1]))
    if idx < len(edges):
        conds.append(Condition(signal=name, op=Op.LT, value=edges[idx]))
    return conds


def mine_patterns(facts: Sequence[EpisodeFacts], policy: BulkPolicy, cfg: Config) -> list[Pattern]:
    min_support = cfg.integer("discovery", "min_support")
    max_len = cfg.integer("discovery", "max_itemset")
    ignored = set(cfg.strings("discovery", "ignored_items"))
    counts: Counter[frozenset[str]] = Counter()
    members: dict[frozenset[str], list[EpisodeFacts]] = defaultdict(list)
    for f in facts:
        usable = sorted(i for i in f.items if i not in ignored)
        for size in range(1, max_len + 1):
            for combo in combinations(usable, size):
                key = frozenset(combo)
                counts[key] += 1
                members[key].append(f)

    risk: list[Pattern] = []
    benign: list[Pattern] = []
    for itemset, support in counts.items():
        if support < min_support:
            continue
        group = members[itemset]
        labelled = [f for f in group if f.label]
        escalated = sum(1 for f in labelled if f.label == "ESCALATED")
        cleared = len(labelled) - escalated
        detected = sum(1 for f in group if f.detected)
        coverage = (Decimal(detected) / Decimal(support)).quantize(Decimal("0.0001"))
        risky = sum(
            1
            for f in group
            if f.label == "ESCALATED" or f.assessed_class is HypothesisClass.ANOMALOUS
        )
        desks = {f.desk for f in group}
        sample = tuple(
            sorted(f.episode_id for f in group)[: cfg.integer("discovery", "sample_size")]
        )
        items = tuple(sorted(itemset))
        if (
            Decimal(risky) / Decimal(support) >= cfg.decimal("discovery", "min_risk_rate")
            and coverage <= cfg.decimal("discovery", "max_rule_coverage")
            and escalated >= cfg.integer("discovery", "min_escalated_labels")
        ):
            risk.append(
                Pattern(
                    pattern_id=stable_id("PAT", "RISK", *items),
                    kind=PatternKind.UNCAPTURED_RISK,
                    items=items,
                    support=support,
                    labelled=len(labelled),
                    escalated=escalated,
                    cleared=cleared,
                    rule_coverage=coverage,
                    benign_verified=0,
                    risk_evidence=risky,
                    sample_episode_ids=sample,
                    scope_desk=next(iter(desks)) if len(desks) == 1 else "*",
                )
            )
        benign_types = Counter(
            f.assessed_type
            for f in group
            if f.assessed_class is HypothesisClass.BENIGN and f.assessed_type
        )
        if not benign_types or len(desks) != 1:
            continue
        top_type, verified = benign_types.most_common(1)[0]
        desk = next(iter(desks))
        if policy_allows(policy, top_type, desk):
            continue
        if (
            escalated == 0
            and cleared >= cfg.integer("discovery", "min_curated_cleared")
            and Decimal(verified) / Decimal(support) >= cfg.decimal("discovery", "min_benign_rate")
        ):
            benign.append(
                Pattern(
                    pattern_id=stable_id("PAT", "BENIGN", top_type, desk, *items),
                    kind=PatternKind.RECURRING_BENIGN,
                    items=(*items, f"hypothesis={top_type}"),
                    support=support,
                    labelled=len(labelled),
                    escalated=0,
                    cleared=cleared,
                    rule_coverage=coverage,
                    benign_verified=verified,
                    sample_episode_ids=sample,
                    scope_desk=desk,
                )
            )

    def closed(patterns: list[Pattern]) -> list[Pattern]:
        """Keep the most specific itemset per member set, then the strongest few."""
        best: dict[frozenset[str], Pattern] = {}
        for p in patterns:
            itemset = frozenset(i for i in p.items if not i.startswith("hypothesis="))
            key = frozenset(f.episode_id for f in members[itemset])
            if key not in best or len(p.items) > len(best[key].items):
                best[key] = p
        ranked = sorted(
            best.values(),
            key=lambda p: (
                -(Decimal(p.risk_evidence + p.benign_verified) / Decimal(p.support)),
                -(p.escalated + p.benign_verified),
                -p.support,
                -len(p.items),
                p.pattern_id,
            ),
        )
        return ranked[: cfg.integer("discovery", "max_patterns_per_kind")]

    return closed(risk) + closed(benign)


def _precision(
    rule: RuleSpec, facts: Sequence[EpisodeFacts], signals: Mapping[str, EpisodeSignals]
) -> tuple[Decimal | None, int, Decimal]:
    """Labelled precision, fire count, and the share of fires production already detects."""
    fires = [f for f in facts if evaluate_rule(rule, signals[f.episode_id]) is not None]
    overlap = (
        (Decimal(sum(f.detected for f in fires)) / Decimal(len(fires))) if fires else Decimal()
    )
    labelled = [f for f in fires if f.label]
    if not labelled:
        return None, len(fires), overlap
    tp = sum(1 for f in labelled if f.label == "ESCALATED")
    return Decimal(tp) / Decimal(len(labelled)), len(fires), overlap


def synthesize_rule(
    pattern: Pattern,
    facts: Sequence[EpisodeFacts],
    signals: Mapping[str, EpisodeSignals],
    cfg: Config,
) -> RuleSpec:
    conditions: list[Condition] = []
    for item in pattern.items:
        conditions.extend(_item_condition(item, cfg))
    rule_id = "DR" + stable_id("", pattern.pattern_id)[1:9].upper()

    def make(conds: Sequence[Condition]) -> RuleSpec:
        return RuleSpec(
            rule_id=rule_id,
            subrule_id=f"{rule_id}.1",
            description=f"Discovered: {'; '.join(pattern.items)}",
            severity=cfg.string("discovery", "candidate_severity"),
            conditions=tuple(conds),
            parameters={},
        )

    best = make(conditions)
    best_precision, best_fires, _ = _precision(best, facts, signals)
    growth = cfg.decimal("discovery", "max_volume_growth")
    max_overlap = cfg.decimal("discovery", "max_rule_coverage")
    for cond in list(conditions):
        trial_conds = [c for c in best.conditions if c != cond]
        if not trial_conds:
            continue
        trial = make(trial_conds)
        precision, fires, overlap = _precision(trial, facts, signals)
        if (
            precision is not None
            and best_precision is not None
            and precision >= best_precision
            and Decimal(fires) <= Decimal(max(best_fires, 1)) * growth
            and overlap <= max_overlap  # stay on activity production does not already catch
        ):
            best, best_precision, best_fires = trial, precision, fires
    return best


def bulk_scope_for(pattern: Pattern) -> BulkScope:
    hypothesis = next(i.split("=", 1)[1] for i in pattern.items if i.startswith("hypothesis="))
    return BulkScope(hypothesis_type=hypothesis, desk=pattern.scope_desk)
