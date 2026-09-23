"""Deterministic claim checkers (rails 5, 7).

To add a claim type: create a module here, decorate a pure function with
`@register("CLAIM_NAME")`, import the module below, then reference CLAIM_NAME from config
`[claims]`. See docs/BUSINESS_CHANGES.md.
"""

from asas.checkers import corrections, rebook, unverifiable
from asas.checkers.registry import CHECKERS, Checker, register, verify_claim

__all__ = [
    "CHECKERS",
    "Checker",
    "corrections",
    "rebook",
    "register",
    "unverifiable",
    "verify_claim",
]
