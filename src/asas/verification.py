"""Deterministic verification of operational explanations against trade data.

Checkers live in `asas.checkers` (one module per claim family). Absence of contradiction is
not confirmation (rail 7): unproven claims are NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS."""

from asas.checkers import CHECKERS, verify_claim

__all__ = ["CHECKERS", "verify_claim"]
