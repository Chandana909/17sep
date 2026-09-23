"""Claim-checker registry. A checker is a pure function; unknown claims are never verified."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from asas.config import Config
from asas.models import ClaimResult, Episode, TradeEvent, Verdict

Checker = Callable[[str, Episode, Sequence[TradeEvent], Config], ClaimResult]
CHECKERS: dict[str, Checker] = {}


def register(name: str) -> Callable[[Checker], Checker]:
    def deco(fn: Checker) -> Checker:
        if name in CHECKERS:
            raise ValueError(f"duplicate claim checker {name}")
        CHECKERS[name] = fn
        return fn

    return deco


def not_verifiable(claim: str, basis: str) -> ClaimResult:
    return ClaimResult(claim, Verdict.NOT_VERIFIABLE, basis)


def verify_claim(
    claim: str, episode: Episode, all_events: Sequence[TradeEvent], cfg: Config
) -> ClaimResult:
    checker = CHECKERS.get(claim)
    if checker is None:
        return not_verifiable(claim, "no deterministic checker for claim")
    return checker(claim, episode, all_events, cfg)
