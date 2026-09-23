"""Episode -> ReviewCase. Any missing decision config fails safe to individual review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from asas.config import Config, ConfigMissing
from asas.evidence import missing_evidence
from asas.ids import stable_id
from asas.models import (
    Alert,
    ClaimResult,
    Episode,
    PastCase,
    ReviewCase,
    RfiEvent,
    TradeEvent,
    Treatment,
)
from asas.priority import compute_priority
from asas.treatment import HistorySignal, history_signal, open_rfi, treatment_reasons
from asas.verification import verify_claim


def _category(alerts: Sequence[Alert], cfg: Config, reasons: list[str]) -> str | None:
    try:
        mapping = cfg.require("categories", "by_alert_type")
    except ConfigMissing as exc:
        reasons.append(f"CONFIG_MISSING:{exc.args[0]}")
        return None
    categories = {mapping.get(a.alert_type) for a in alerts}
    if None in categories:
        reasons.append("CATEGORY_UNMAPPED")
        return None
    if len(categories) > 1:
        reasons.append("CATEGORY_CONFLICT")
        return None
    return str(categories.pop())


def build_case(
    episode: Episode,
    alerts_by_id: Mapping[str, Alert],
    all_events: Sequence[TradeEvent],
    rfi_events: Sequence[RfiEvent],
    past_cases: Sequence[PastCase],
    cfg: Config,
    as_of: datetime,
) -> ReviewCase:
    alerts = [alerts_by_id[i] for i in episode.alert_ids]
    reasons: list[str] = []
    category = _category(alerts, cfg, reasons)
    claims: tuple[ClaimResult, ...] = ()
    evidence: tuple[str, ...] = ()
    history: HistorySignal | None = None
    if category is not None:
        try:
            claims = tuple(
                verify_claim(c, episode, all_events, cfg) for c in cfg.strings("claims", category)
            )
            evidence = missing_evidence(
                cfg.strings("evidence", "required", category), episode, claims, alerts
            )
            history = history_signal(category, past_cases, as_of, cfg)
        except ConfigMissing as exc:
            reasons.append(f"CONFIG_MISSING:{exc.args[0]}")
    rfi_is_open = open_rfi(episode.alert_ids, rfi_events)
    reasons.extend(
        treatment_reasons(
            lifecycle_issues=episode.lifecycle_issues,
            claims=claims,
            evidence_missing=evidence,
            rfi_is_open=rfi_is_open,
            history=history,
        )
    )
    priority: Decimal | None
    try:
        priority = compute_priority(episode, claims, cfg)
    except ConfigMissing as exc:
        priority = None
        reasons.append(f"CONFIG_MISSING:{exc.args[0]}")
    unique = tuple(dict.fromkeys(reasons))
    return ReviewCase(
        case_id=stable_id("CASE", episode.episode_id),
        episode=episode,
        category=category,
        claims=claims,
        evidence_missing=evidence,
        open_rfi=rfi_is_open,
        history_comparable=history.comparable if history else 0,
        history_adverse=history.adverse if history else 0,
        treatment=Treatment.INDIVIDUAL_REVIEW if unique else Treatment.BULK_CANDIDATE,
        reasons=unique,
        priority=priority,
    )
