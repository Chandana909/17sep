"""Claims that the field contract cannot prove (rail 7). They exist so the category is
recognised and routed to individual review with an explicit reason, never silently."""

from __future__ import annotations

from collections.abc import Sequence

from asas.checkers.registry import not_verifiable, register
from asas.config import Config
from asas.models import ClaimResult, Episode, TradeEvent


@register("LATE_BOOKING")
def late_booking(
    claim: str, _episode: Episode, _all: Sequence[TradeEvent], _cfg: Config
) -> ClaimResult:
    return not_verifiable(claim, "execution time is not in the field contract")
