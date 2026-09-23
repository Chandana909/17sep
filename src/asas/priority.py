"""Deterministic priority from config weights. Person fields are not inputs (rail 11)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from asas.config import Config
from asas.models import ClaimResult, Episode, Verdict


def max_notional(episode: Episode) -> Decimal:
    values = [
        abs(e.price * e.quantity)
        for e in episode.events
        if e.price is not None and e.quantity is not None
    ]
    return max(values, default=Decimal())


def compute_priority(episode: Episode, claims: Sequence[ClaimResult], cfg: Config) -> Decimal:
    features = {
        "alert_count": Decimal(len(episode.alert_ids)),
        "notional": max_notional(episode),
        "contradicted_claims": Decimal(sum(c.verdict is Verdict.CONTRADICTED for c in claims)),
        "not_verifiable_claims": Decimal(sum(c.verdict is Verdict.NOT_VERIFIABLE for c in claims)),
    }
    return sum(
        (cfg.decimal("priority", "weights", name) * value for name, value in features.items()),
        Decimal(),
    )
