"""Drift monitoring: does this run look like the ones before it?

A surveillance system can go wrong quietly: an upstream change doubles alert volume, a model
update starts abstaining, a policy release makes everything bulk-eligible. After every
pipeline run the KPIs below are recorded, then compared with the median of the previous
`monitoring.history_runs` runs:
- rates: bulk, escalation, abstention, unexplained deviation, agent failure, model fallback
- volumes: cases, and alerts per rule in the review window

Rates are compared on absolute change (`monitoring.rate_tolerance`) and volumes on relative
change (`monitoring.volume_tolerance`). Flags are reported in the API, the metrics and the
run log. They inform people and never change a decision.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from asas.core.config import Config
from asas.domain.models import Case, Category, InvestigationResult, Recommendation
from asas.engine.snapshot import Snapshot

_Q = Decimal("0.0001")
RATES = (
    "bulk_rate",
    "escalation_rate",
    "abstention_rate",
    "unexplained_rate",
    "agent_failure_rate",
    "fallback_rate",
)


def _rate(num: int, den: int) -> Decimal:
    return (Decimal(num) / Decimal(den)).quantize(_Q) if den else Decimal(0)


class RunKpis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_key: str
    as_of: datetime
    cases: int
    rates: dict[str, Decimal]
    alerts_by_rule: dict[str, int]


class DriftFlag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metric: str
    current: Decimal
    baseline: Decimal
    change: Decimal
    kind: str  # rate | volume


class DriftReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_key: str
    history_runs: int
    status: str  # OK | DRIFT | INSUFFICIENT_HISTORY
    flags: tuple[DriftFlag, ...]


def run_kpis(
    run_key: str,
    as_of: datetime,
    cases: Sequence[Case],
    results: Sequence[InvestigationResult | None],
    snap: Snapshot,
    cfg: Config,
) -> RunKpis:
    n = len(cases)
    completed = [r for r in results if r is not None]
    review_start = as_of - timedelta(days=cfg.integer("pipeline", "review_window_days"))
    alerts = Counter(a.rule_id for a in snap.alerts_by_id.values() if a.alert_time > review_start)
    return RunKpis(
        run_key=run_key,
        as_of=as_of,
        cases=n,
        rates={
            "bulk_rate": _rate(
                sum(c.recommendation is Recommendation.PROPOSED_BULK for c in cases), n
            ),
            "escalation_rate": _rate(
                sum(c.recommendation is Recommendation.ESCALATION_RECOMMENDED for c in cases), n
            ),
            "abstention_rate": _rate(
                sum(any(r.startswith("ABSTAINED") for r in c.reasons) for c in cases), n
            ),
            "unexplained_rate": _rate(
                sum(
                    c.classification is not None
                    and c.classification.category is Category.UNEXPLAINED_DEVIATION
                    for c in cases
                ),
                n,
            ),
            "agent_failure_rate": _rate(len(results) - len(completed), len(results)),
            "fallback_rate": _rate(sum(r.fallback_used for r in completed), len(completed)),
        },
        alerts_by_rule=dict(sorted(alerts.items())),
    )


def drift(current: RunKpis, history: Sequence[RunKpis], cfg: Config) -> DriftReport:
    previous = [h for h in history if h.run_key != current.run_key]
    previous = previous[-cfg.integer("monitoring", "history_runs") :]
    if len(previous) < cfg.integer("monitoring", "min_history"):
        return DriftReport(
            run_key=current.run_key,
            history_runs=len(previous),
            status="INSUFFICIENT_HISTORY",
            flags=(),
        )
    rate_tol = cfg.decimal("monitoring", "rate_tolerance")
    volume_tol = cfg.decimal("monitoring", "volume_tolerance")
    flags: list[DriftFlag] = []

    def median(values: Sequence[Decimal]) -> Decimal:
        return Decimal(str(statistics.median(values))).quantize(_Q)

    for name in RATES:
        base = median([h.rates.get(name, Decimal(0)) for h in previous])
        now = current.rates.get(name, Decimal(0))
        if abs(now - base) > rate_tol:
            flags.append(
                DriftFlag(metric=name, current=now, baseline=base, change=now - base, kind="rate")
            )

    volumes: Mapping[str, tuple[int, list[int]]] = {
        "cases": (current.cases, [h.cases for h in previous]),
        **{
            f"alerts:{rule}": (
                current.alerts_by_rule.get(rule, 0),
                [h.alerts_by_rule.get(rule, 0) for h in previous],
            )
            for rule in sorted({r for h in (current, *previous) for r in h.alerts_by_rule})
        },
    }
    for name, (value, past) in volumes.items():
        base = median([Decimal(v) for v in past])
        now = Decimal(value)
        change = ((now - base) / base).quantize(_Q) if base else (Decimal(1) if now else Decimal(0))
        if abs(change) > volume_tol:
            flags.append(
                DriftFlag(metric=name, current=now, baseline=base, change=change, kind="volume")
            )
    return DriftReport(
        run_key=current.run_key,
        history_runs=len(previous),
        status="DRIFT" if flags else "OK",
        flags=tuple(flags),
    )
