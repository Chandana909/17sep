"""Business-level reports. Values enter prose only via `{{F<n>}}` fact placeholders
filled by `render`; deterministic templates are the agent-disabled fallback."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from asas.models import AlertAnnex, CohortProposal, ReviewCase, Verdict

PLACEHOLDER = re.compile(r"\{\{(F\d+)\}\}")


@dataclass(frozen=True, slots=True)
class Fact:
    fact_id: str
    label: str
    value: str


class _Facts:
    def __init__(self) -> None:
        self.items: list[Fact] = []

    def add(self, label: str, value: object) -> str:
        fid = f"F{len(self.items) + 1}"
        self.items.append(Fact(fid, label, str(value)))
        return "{{" + fid + "}}"


def render(template: str, facts: Sequence[Fact]) -> str:
    values = {f.fact_id: f.value for f in facts}

    def sub(match: re.Match[str]) -> str:
        if match.group(1) not in values:
            raise KeyError(f"unknown fact {match.group(1)}")
        return values[match.group(1)]

    return PLACEHOLDER.sub(sub, template)


def case_report(
    case: ReviewCase, annexes: Sequence[AlertAnnex], entitled: bool
) -> tuple[str, tuple[Fact, ...]]:
    f = _Facts()
    lines = [
        f"Review case {f.add('Case ID', case.case_id)} on trade "
        f"{f.add('Trade ID', case.episode.trade_id)} links "
        f"{f.add('Alert count', len(case.episode.alert_ids))} alert(s).",
        f"Category: {f.add('Category', case.category or 'UNRESOLVED')}.",
        f"Proposed treatment: {f.add('Treatment', case.treatment.value)} "
        f"(priority {f.add('Priority', case.priority if case.priority is not None else 'UNSET')}).",
    ]
    for claim in case.claims:
        lines.append(
            f"Explanation {f.add('Claim', claim.claim)}: {f.add('Verdict', claim.verdict.value)} "
            f"({f.add('Basis', claim.basis)})."
        )
    if case.reasons:
        lines.append(f"Individual review reasons: {f.add('Reasons', ', '.join(case.reasons))}.")
    if entitled:
        persons = sorted({f"{k}={v}" for a in annexes for k, v in a.persons if v})
        if persons:
            lines.append(f"People: {f.add('People', ', '.join(persons))}.")
    lines.append("Decision rests with the supervisor.")
    return "\n".join(lines), tuple(f.items)


def cohort_report(cohort: CohortProposal) -> tuple[str, tuple[Fact, ...]]:
    f = _Facts()
    text = (
        f"Proposed bulk-review cohort {f.add('Cohort ID', cohort.cohort_id)}: "
        f"{f.add('Case count', len(cohort.case_ids))} case(s) in category "
        f"{f.add('Category', cohort.category)} with identical verified evidence "
        f"({f.add('Signature', '; '.join(cohort.signature))}).\n"
        f"Status: {f.add('Status', cohort.status)}. Members: "
        f"{f.add('Case IDs', ', '.join(cohort.case_ids))}.\n"
        "Supervisor attestation is required; no case has been signed off."
    )
    return text, tuple(f.items)


def rfi_draft(case: ReviewCase) -> tuple[str, tuple[Fact, ...]] | None:
    open_claims = [c for c in case.claims if c.verdict is not Verdict.VERIFIED]
    if not open_claims:
        return None
    f = _Facts()
    lines = [
        "DRAFT - NOT SENT. Request for information on trade "
        f"{f.add('Trade ID', case.episode.trade_id)} "
        f"(case {f.add('Case ID', case.case_id)})."
    ]
    for claim in open_claims:
        lines.append(
            f"Please provide evidence for {f.add('Claim', claim.claim)}: "
            f"{f.add('Basis', claim.basis)}."
        )
    return "\n".join(lines), tuple(f.items)
