"""Evidence-complete bulk-review cohort proposals. Nothing is removed from review
(rail 2): every case ends up either in a proposed cohort or in the individual queue."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Sequence

from asas.config import Config, ConfigMissing
from asas.ids import stable_id
from asas.models import CohortProposal, ReviewCase, Treatment


def _to_individual(case: ReviewCase, reason: str) -> ReviewCase:
    return dataclasses.replace(
        case, treatment=Treatment.INDIVIDUAL_REVIEW, reasons=(*case.reasons, reason)
    )


def _signature(case: ReviewCase) -> tuple[str, ...]:
    return tuple(sorted(f"{c.claim}={c.verdict.value}" for c in case.claims))


def propose_cohorts(
    cases: Sequence[ReviewCase], cfg: Config
) -> tuple[tuple[CohortProposal, ...], tuple[ReviewCase, ...]]:
    try:
        min_size = cfg.integer("cohorts", "min_size")
        max_size = cfg.integer("cohorts", "max_size")
        if min_size < 1 or max_size < min_size:
            raise ConfigMissing("cohorts.min_size|max_size")
    except ConfigMissing as exc:
        reason = f"CONFIG_MISSING:{exc.args[0]}"
        return (), tuple(
            _to_individual(c, reason) if c.treatment is Treatment.BULK_CANDIDATE else c
            for c in cases
        )

    groups: dict[tuple[str, tuple[str, ...]], list[ReviewCase]] = defaultdict(list)
    final: dict[str, ReviewCase] = {}
    for case in cases:
        if case.treatment is Treatment.BULK_CANDIDATE and case.category is not None:
            groups[(case.category, _signature(case))].append(case)
        else:
            final[case.case_id] = case

    cohorts: list[CohortProposal] = []
    for (category, signature), members in sorted(groups.items()):
        members.sort(key=lambda c: c.case_id)
        if len(members) < min_size:
            for c in members:
                final[c.case_id] = _to_individual(c, "COHORT_BELOW_MIN_SIZE")
            continue
        for start in range(0, len(members), max_size):
            chunk = members[start : start + max_size]
            ids = tuple(c.case_id for c in chunk)
            if len(chunk) < min_size:
                for c in chunk:
                    final[c.case_id] = _to_individual(c, "COHORT_BELOW_MIN_SIZE")
                continue
            cohorts.append(CohortProposal(stable_id("CH", *ids), category, signature, ids))
            for c in chunk:
                final[c.case_id] = c
    return tuple(cohorts), tuple(final[k] for k in sorted(final))
