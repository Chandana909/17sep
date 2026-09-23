"""Deterministic review treatment. Bulk only when no reason for individual review exists.
History can add a reason (raise attention) but can never remove one (rail 9)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from asas.config import Config
from asas.models import ClaimResult, PastCase, RfiAction, RfiEvent, Verdict


@dataclass(frozen=True, slots=True)
class HistorySignal:
    comparable: int
    adverse: int
    forces_individual: bool


def history_signal(
    category: str, past_cases: Iterable[PastCase], as_of: datetime, cfg: Config
) -> HistorySignal:
    lookback = timedelta(days=cfg.integer("history", "lookback_days"))
    min_n = cfg.integer("history", "min_comparable")
    adverse_outcomes = set(cfg.strings("history", "adverse_outcomes"))
    threshold = cfg.decimal("history", "adverse_rate_threshold")
    comparable = [
        p for p in past_cases if p.category == category and as_of - lookback <= p.decided_at < as_of
    ]
    adverse = sum(1 for p in comparable if p.outcome in adverse_outcomes)
    forces = len(comparable) >= min_n and Decimal(adverse) / Decimal(len(comparable)) >= threshold
    return HistorySignal(len(comparable), adverse, forces)


def open_rfi(alert_ids: Iterable[str], rfi_events: Iterable[RfiEvent]) -> bool:
    """Rail 8 exception: open-RFI status from events already filtered to record_time <= as_of."""
    ids = set(alert_ids)
    latest: dict[str, RfiEvent] = {}
    for event in sorted(rfi_events, key=lambda e: (e.record_time, e.action.value)):
        if event.alert_id in ids:
            latest[event.alert_id] = event
    return any(e.action is RfiAction.OPENED for e in latest.values())


def treatment_reasons(
    *,
    lifecycle_issues: Sequence[str],
    claims: Sequence[ClaimResult],
    evidence_missing: Sequence[str],
    rfi_is_open: bool,
    history: HistorySignal | None,
) -> list[str]:
    reasons = [f"LIFECYCLE:{issue}" for issue in lifecycle_issues]
    if not claims:
        reasons.append("NO_VERIFIED_EXPLANATION")
    reasons.extend(
        f"CLAIM_{c.verdict.value}:{c.claim}" for c in claims if c.verdict is not Verdict.VERIFIED
    )
    reasons.extend(f"EVIDENCE_MISSING:{item}" for item in evidence_missing)
    if rfi_is_open:
        reasons.append("OPEN_RFI")
    if history is not None and history.forces_individual:
        reasons.append("HISTORY_ADVERSE_COMPARABLES")
    return reasons
