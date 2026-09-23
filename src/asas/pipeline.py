"""End-to-end run at a point in time. Decisions are fully deterministic; agents only
touch `case_reports`, `cohort_reports` and `rfi_drafts` (rail 15)."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from asas.agents.gateway import ModelGateway
from asas.agents.manifest import DecisionManifest, ManifestLog
from asas.agents.narrator import Narrator
from asas.agents.tools import ToolRegistry
from asas.cases import build_case
from asas.cohorts import propose_cohorts
from asas.config import Config, ConfigMissing
from asas.fields import ALIAS_MAPPING_VERSION, FIELD_CONTRACT_VERSION
from asas.linking import link_episodes
from asas.models import AlertAnnex, CohortProposal, ReviewCase, Treatment
from asas.report import case_report, cohort_report, rfi_draft
from asas.serialize import canonical_json
from asas.sources import ReadOnlySource, assert_pit


@dataclass(frozen=True)
class RunResult:
    as_of: datetime
    config_version: str
    cases: tuple[ReviewCase, ...]
    cohorts: tuple[CohortProposal, ...]
    case_reports: dict[str, str]
    cohort_reports: dict[str, str]
    rfi_drafts: dict[str, str]
    manifests: tuple[DecisionManifest, ...]

    @property
    def individual_queue(self) -> tuple[str, ...]:
        """Highest priority first; unknown priority (config gap) first of all."""
        individual = [c for c in self.cases if c.treatment is Treatment.INDIVIDUAL_REVIEW]
        individual.sort(
            key=lambda c: (c.priority is not None, -(c.priority or Decimal()), c.case_id)
        )
        return tuple(c.case_id for c in individual)

    def decisions(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "config_version": self.config_version,
            "field_contract_version": FIELD_CONTRACT_VERSION,
            "alias_mapping_version": ALIAS_MAPPING_VERSION,
            "cases": self.cases,
            "cohorts": self.cohorts,
            "individual_queue": self.individual_queue,
        }

    def decisions_json(self) -> str:
        return canonical_json(self.decisions())


def run(
    source: ReadOnlySource,
    cfg: Config,
    as_of: datetime,
    gateway: ModelGateway | None = None,
    entitled: bool = False,
) -> RunResult:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    alerts = source.alerts(as_of)
    annexes = source.annexes(as_of)
    events = source.trade_events(as_of)
    rfis = source.rfi_events(as_of)
    past = source.past_cases(as_of)
    for records in (alerts, annexes, events, rfis):
        assert_pit(records, as_of)
    if any(p.decided_at >= as_of for p in past):
        raise ValueError("past case decided at or after as_of")

    window_missing = False
    try:
        window = timedelta(hours=cfg.integer("linking", "window_hours"))
    except ConfigMissing:
        window_missing = True
        window = timedelta()  # fail safe: no cross-alert linking, every alert its own episode
    episodes = link_episodes(alerts, events, window)
    alerts_by_id = {a.alert_id: a for a in alerts}
    raw_cases = [build_case(e, alerts_by_id, events, rfis, past, cfg, as_of) for e in episodes]
    if window_missing:
        raw_cases = [_flag_missing_window(c) for c in raw_cases]
    cohorts, cases = propose_cohorts(raw_cases, cfg)

    annex_by_alert: dict[str, list[AlertAnnex]] = defaultdict(list)
    for annex in annexes:
        annex_by_alert[annex.alert_id].append(annex)
    case_by_id = {c.case_id: c for c in cases}
    tools = ToolRegistry(
        {"get_case": lambda cid: canonical_json(case_by_id[cid]) if cid in case_by_id else ""}
    )
    log = ManifestLog()
    narrator = Narrator(gateway, cfg, as_of, log, tools)

    case_reports: dict[str, str] = {}
    rfi_drafts: dict[str, str] = {}
    for case in cases:
        case_annexes = [x for aid in case.episode.alert_ids for x in annex_by_alert[aid]]
        texts = [t for aid in case.episode.alert_ids if (t := alerts_by_id[aid].explanation_text)]
        template, facts = case_report(case, case_annexes, entitled)
        case_reports[case.case_id] = narrator.write(
            "case_summary", case.case_id, template, facts, texts
        )
        draft = rfi_draft(case)
        if draft is not None:
            rfi_drafts[case.case_id] = narrator.write(
                "rfi_draft", case.case_id, draft[0], draft[1], texts
            )
    cohort_reports: dict[str, str] = {}
    for cohort in cohorts:
        template, facts = cohort_report(cohort)
        cohort_reports[cohort.cohort_id] = narrator.write(
            "cohort_summary", cohort.cohort_id, template, facts
        )

    return RunResult(
        as_of=as_of,
        config_version=cfg.version,
        cases=cases,
        cohorts=cohorts,
        case_reports=case_reports,
        cohort_reports=cohort_reports,
        rfi_drafts=rfi_drafts,
        manifests=log.entries,
    )


def _flag_missing_window(case: ReviewCase) -> ReviewCase:
    return dataclasses.replace(
        case,
        treatment=Treatment.INDIVIDUAL_REVIEW,
        reasons=(*case.reasons, "CONFIG_MISSING:linking.window_hours"),
    )
