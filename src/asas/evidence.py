"""Evidence readiness. Free text alone is never evidence; it only counts alongside
verified claims."""

from __future__ import annotations

from collections.abc import Sequence

from asas.models import Alert, ClaimResult, Episode, Verdict


def missing_evidence(
    required: Sequence[str],
    episode: Episode,
    claims: Sequence[ClaimResult],
    alerts: Sequence[Alert],
) -> tuple[str, ...]:
    satisfied = {
        "TRADE_LIFECYCLE_VALID": not episode.lifecycle_issues,
        "ALL_CLAIMS_VERIFIED": bool(claims) and all(c.verdict is Verdict.VERIFIED for c in claims),
        "EXPLANATION_PRESENT": any((a.explanation_text or "").strip() for a in alerts),
    }
    return tuple(item for item in required if not satisfied.get(item, False))
