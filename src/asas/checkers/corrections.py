"""Amendment-correction claims: an amendment changed a value, within a config tolerance."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from asas.checkers.registry import Checker, not_verifiable, register
from asas.config import Config
from asas.models import ClaimResult, Episode, EventType, TradeEvent, Verdict


def rel_change(old: Decimal, new: Decimal) -> Decimal:
    return abs(new - old) / abs(old)


def amendment_correction(attr: str, tolerance_key: str) -> Checker:
    def check(claim: str, episode: Episode, _all: Sequence[TradeEvent], cfg: Config) -> ClaimResult:
        max_rel = cfg.decimal("verification", tolerance_key)
        results: list[ClaimResult] = []
        last: Decimal | None = None
        for event in episode.events:
            value: Decimal | None = getattr(event, attr)
            if event.event_type is EventType.AMEND:
                if last is None or value is None or not last:
                    results.append(not_verifiable(claim, f"amendment lacks comparable {attr}"))
                elif value == last:
                    results.append(
                        ClaimResult(claim, Verdict.CONTRADICTED, f"amendment left {attr} unchanged")
                    )
                elif rel_change(last, value) > max_rel:
                    results.append(
                        ClaimResult(
                            claim,
                            Verdict.CONTRADICTED,
                            f"{attr} change exceeds correction tolerance",
                        )
                    )
                else:
                    results.append(
                        ClaimResult(claim, Verdict.VERIFIED, f"{attr} amendment within tolerance")
                    )
            if value is not None:
                last = value
        if not results:
            return not_verifiable(claim, "no amendment event visible")
        for verdict in (Verdict.CONTRADICTED, Verdict.NOT_VERIFIABLE):
            for result in results:
                if result.verdict is verdict:
                    return result
        return results[0]

    return check


register("PRICE_CORRECTION")(amendment_correction("price", "price_correction_max_rel_change"))
register("QUANTITY_CORRECTION")(
    amendment_correction("quantity", "quantity_correction_max_rel_change")
)
