"""Cancel-and-rebook claim: the cancelled trade was replaced by an equivalent new trade."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from asas.checkers.corrections import rel_change
from asas.checkers.registry import not_verifiable, register
from asas.config import Config
from asas.models import ClaimResult, Episode, EventType, TradeEvent, Verdict


def _compatible(candidate: TradeEvent, cancel: TradeEvent, side: str | None) -> bool:
    if candidate.original_trade_id is not None and candidate.original_trade_id != cancel.trade_id:
        return False
    return side is None or candidate.side is None or candidate.side == side


@register("CANCEL_REBOOK")
def cancel_rebook(
    claim: str, episode: Episode, all_events: Sequence[TradeEvent], cfg: Config
) -> ClaimResult:
    window = timedelta(hours=cfg.integer("verification", "rebook_window_hours"))
    tolerance = cfg.decimal("verification", "rebook_quantity_rel_tolerance")
    cancels = [e for e in episode.events if e.event_type is EventType.CANCEL]
    if not cancels:
        return not_verifiable(claim, "no cancellation visible")
    cancel = cancels[0]
    before = [e for e in episode.events if e is not cancel]
    quantity = next((e.quantity for e in reversed(before) if e.quantity is not None), None)
    side = next((e.side for e in reversed(before) if e.side is not None), None)
    if quantity is None or not quantity:
        return not_verifiable(claim, "cancelled quantity unknown")
    candidates = [
        e
        for e in all_events
        if e.event_type is EventType.NEW
        and e.trade_id != episode.trade_id
        and e.instrument_id == cancel.instrument_id
        and e.book_id == cancel.book_id
        and cancel.event_time <= e.event_time <= cancel.event_time + window
        and e.quantity is not None
        and _compatible(e, cancel, side)
    ]
    if not candidates:
        return not_verifiable(claim, "no compatible rebook visible in window")
    if any(
        c.quantity is not None and rel_change(quantity, c.quantity) <= tolerance for c in candidates
    ):
        return ClaimResult(claim, Verdict.VERIFIED, "matching rebook found in window")
    return ClaimResult(claim, Verdict.CONTRADICTED, "rebook quantity differs from cancelled trade")
