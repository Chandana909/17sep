"""Deterministic verification of operational explanations against trade data.

Absence of contradiction is not confirmation (rail 7): anything not positively proven from
contract fields is NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS. Config gaps raise ConfigMissing."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal

from asas.config import Config
from asas.models import ClaimResult, Episode, EventType, TradeEvent, Verdict

Checker = Callable[[str, Episode, Sequence[TradeEvent], Config], ClaimResult]


def _nv(claim: str, basis: str) -> ClaimResult:
    return ClaimResult(claim, Verdict.NOT_VERIFIABLE, basis)


def _rel_change(old: Decimal, new: Decimal) -> Decimal:
    return abs(new - old) / abs(old)


def _price_correction(
    claim: str, episode: Episode, _all: Sequence[TradeEvent], cfg: Config
) -> ClaimResult:
    max_rel = cfg.decimal("verification", "price_correction_max_rel_change")
    verdicts: list[ClaimResult] = []
    last_price: Decimal | None = None
    for event in episode.events:
        if event.event_type is EventType.AMEND:
            if last_price is None or event.price is None or not last_price:
                verdicts.append(_nv(claim, "amendment lacks comparable prices"))
            else:
                change = _rel_change(last_price, event.price)
                if not change:
                    verdicts.append(
                        ClaimResult(claim, Verdict.CONTRADICTED, "amendment left price unchanged")
                    )
                elif change > max_rel:
                    verdicts.append(
                        ClaimResult(
                            claim, Verdict.CONTRADICTED, "price change exceeds correction tolerance"
                        )
                    )
                else:
                    verdicts.append(
                        ClaimResult(
                            claim, Verdict.VERIFIED, "amendment within correction tolerance"
                        )
                    )
        if event.price is not None:
            last_price = event.price
    if not verdicts:
        return _nv(claim, "no amendment event visible")
    for verdict in (Verdict.CONTRADICTED, Verdict.NOT_VERIFIABLE):
        for result in verdicts:
            if result.verdict is verdict:
                return result
    return verdicts[0]


def _cancel_rebook(
    claim: str, episode: Episode, all_events: Sequence[TradeEvent], cfg: Config
) -> ClaimResult:
    window = timedelta(hours=cfg.integer("verification", "rebook_window_hours"))
    tolerance = cfg.decimal("verification", "rebook_quantity_rel_tolerance")
    cancels = [e for e in episode.events if e.event_type is EventType.CANCEL]
    if not cancels:
        return _nv(claim, "no cancellation visible")
    cancel = cancels[0]
    quantity = next(
        (e.quantity for e in reversed(episode.events) if e.quantity is not None and e != cancel),
        None,
    )
    if quantity is None or not quantity:
        return _nv(claim, "cancelled quantity unknown")
    candidates = [
        e
        for e in all_events
        if e.event_type is EventType.NEW
        and e.trade_id != episode.trade_id
        and e.instrument_id == cancel.instrument_id
        and e.book_id == cancel.book_id
        and cancel.event_time <= e.event_time <= cancel.event_time + window
        and e.quantity is not None
    ]
    if not candidates:
        return _nv(claim, "no rebook visible in window")
    if any(
        c.quantity is not None and _rel_change(quantity, c.quantity) <= tolerance
        for c in candidates
    ):
        return ClaimResult(claim, Verdict.VERIFIED, "matching rebook found in window")
    return ClaimResult(claim, Verdict.CONTRADICTED, "rebook quantity differs from cancelled trade")


def _late_booking(
    claim: str, _episode: Episode, _all: Sequence[TradeEvent], _cfg: Config
) -> ClaimResult:
    return _nv(claim, "execution time is not in the field contract")


CHECKERS: dict[str, Checker] = {
    "PRICE_CORRECTION": _price_correction,
    "CANCEL_REBOOK": _cancel_rebook,
    "LATE_BOOKING": _late_booking,
}


def verify_claim(
    claim: str, episode: Episode, all_events: Sequence[TradeEvent], cfg: Config
) -> ClaimResult:
    checker = CHECKERS.get(claim)
    if checker is None:
        return _nv(claim, "no deterministic checker for claim")
    return checker(claim, episode, all_events, cfg)
