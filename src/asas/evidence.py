"""Evidence readiness. Items are a registry of pure predicates; config lists which items
each category requires. Unknown items are treated as missing (fail safe). Free text alone
is never evidence; it only counts alongside verified claims."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from asas.models import Alert, ClaimResult, Episode, EventType, Verdict

EvidenceCheck = Callable[[Episode, Sequence[ClaimResult], Sequence[Alert]], bool]
EVIDENCE_ITEMS: dict[str, EvidenceCheck] = {}


def register_evidence(name: str) -> Callable[[EvidenceCheck], EvidenceCheck]:
    def deco(fn: EvidenceCheck) -> EvidenceCheck:
        if name in EVIDENCE_ITEMS:
            raise ValueError(f"duplicate evidence item {name}")
        EVIDENCE_ITEMS[name] = fn
        return fn

    return deco


@register_evidence("TRADE_LIFECYCLE_VALID")
def _lifecycle(episode: Episode, _c: Sequence[ClaimResult], _a: Sequence[Alert]) -> bool:
    return not episode.lifecycle_issues


@register_evidence("ALL_CLAIMS_VERIFIED")
def _claims(_e: Episode, claims: Sequence[ClaimResult], _a: Sequence[Alert]) -> bool:
    return bool(claims) and all(c.verdict is Verdict.VERIFIED for c in claims)


@register_evidence("EXPLANATION_PRESENT")
def _explanation(_e: Episode, _c: Sequence[ClaimResult], alerts: Sequence[Alert]) -> bool:
    return any((a.explanation_text or "").strip() for a in alerts)


@register_evidence("ECONOMICS_PRESENT")
def _economics(episode: Episode, _c: Sequence[ClaimResult], _a: Sequence[Alert]) -> bool:
    live = [e for e in episode.events if e.event_type is not EventType.CANCEL]
    return bool(live) and all(e.price is not None and e.quantity is not None for e in live)


def missing_evidence(
    required: Sequence[str],
    episode: Episode,
    claims: Sequence[ClaimResult],
    alerts: Sequence[Alert],
) -> tuple[str, ...]:
    missing: list[str] = []
    for item in required:
        check = EVIDENCE_ITEMS.get(item)
        if check is None or not check(episode, claims, alerts):
            missing.append(item)
    return tuple(missing)
