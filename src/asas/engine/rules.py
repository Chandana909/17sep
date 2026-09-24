"""Constrained declarative detection rules.

A rule is a conjunction of conditions over named signals, with parameters referenced as
`param`. No code, no free-form expressions: the DSL is small enough to validate, replay,
diff and attack. Agents may only ever propose rules in this form.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from asas.core.config import Config
from asas.core.errors import DataContractError
from asas.domain.models import Condition, Detection, EpisodeSignals, Op, RuleSet, RuleSpec
from asas.engine.signals import signal_kind


def validate_rule(rule: RuleSpec, cfg: Config) -> list[str]:
    problems: list[str] = []
    if not rule.conditions:
        problems.append("rule needs at least one condition")
    if len(rule.conditions) > cfg.integer("rules", "max_conditions"):
        problems.append("too many conditions")
    if rule.severity not in cfg.section("scoring", "rule_severity_weights"):
        problems.append(f"unknown severity {rule.severity}")
    for c in rule.conditions:
        kind = signal_kind(c.signal)
        if kind is None:
            problems.append(f"unknown signal {c.signal}")
            continue
        if (c.value is None) == (c.param is None):
            problems.append(f"{c.signal}: exactly one of value/param required")
            continue
        raw = rule.parameters.get(c.param, "") if c.param else c.value or ""
        if c.param and c.param not in rule.parameters:
            problems.append(f"{c.signal}: parameter {c.param} not defined")
            continue
        if kind == "numeric":
            if c.op is Op.IN:
                problems.append(f"{c.signal}: 'in' not allowed on numeric signals")
            try:
                Decimal(raw)
            except InvalidOperation:
                problems.append(f"{c.signal}: {raw!r} is not a number")
        elif kind == "flag":
            if c.op not in (Op.EQ, Op.NE) or raw not in ("true", "false"):
                problems.append(f"{c.signal}: flags support eq/ne true/false only")
        elif c.op not in (Op.EQ, Op.NE, Op.IN):
            problems.append(f"{c.signal}: labels support eq/ne/in only")
    return problems


def _condition_holds(c: Condition, params: Mapping[str, str], signals: EpisodeSignals) -> bool:
    raw = params[c.param] if c.param else (c.value or "")
    kind = signal_kind(c.signal)
    actual = signals.get(c.signal)
    if actual is None:
        return False
    if kind == "numeric" and isinstance(actual, Decimal):
        target = Decimal(raw)
        return {
            Op.GT: actual > target,
            Op.GE: actual >= target,
            Op.LT: actual < target,
            Op.LE: actual <= target,
            Op.EQ: actual == target,
            Op.NE: actual != target,
        }.get(c.op, False)
    if kind == "flag" and isinstance(actual, bool):
        wanted = raw == "true"
        return (actual == wanted) if c.op is Op.EQ else (actual != wanted)
    if kind == "label" and isinstance(actual, str):
        if c.op is Op.IN:
            return actual in {v.strip() for v in raw.split(",")}
        return (actual == raw) if c.op is Op.EQ else (actual != raw)
    return False


def describe(c: Condition, params: Mapping[str, str]) -> str:
    raw = params[c.param] if c.param else c.value
    return f"{c.signal} {c.op.value} {raw}"


def evaluate_rule(rule: RuleSpec, signals: EpisodeSignals) -> Detection | None:
    matched = []
    for c in rule.conditions:
        if not _condition_holds(c, rule.parameters, signals):
            return None
        matched.append(describe(c, rule.parameters))
    return Detection(
        rule_id=rule.rule_id,
        subrule_id=rule.subrule_id,
        episode_id=signals.episode_id,
        matched=tuple(matched),
    )


def evaluate_ruleset(rules: Sequence[RuleSpec], signals: EpisodeSignals) -> tuple[Detection, ...]:
    return tuple(d for r in rules if (d := evaluate_rule(r, signals)) is not None)


def load_ruleset(path: str | Path, cfg: Config) -> RuleSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ruleset = RuleSet.model_validate(data)
    for rule in ruleset.rules:
        problems = validate_rule(rule, cfg)
        if problems:
            raise DataContractError(f"rule {rule.subrule_id} invalid: {problems}")
    return ruleset
