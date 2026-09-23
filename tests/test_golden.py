"""Golden lifecycle archetypes: expected treatment and reasons per trade."""

from __future__ import annotations

from asas import run
from asas.models import Treatment
from factories import AS_OF, alert, cfg, corrected_trade, event, rfi, source

BULK = Treatment.BULK_CANDIDATE
IND = Treatment.INDIVIDUAL_REVIEW


def _by_trade(result):  # type: ignore[no-untyped-def]
    return {c.episode.trade_id: (c.treatment, c.reasons) for c in result.cases}


def test_archetypes() -> None:
    a1, e1 = corrected_trade("T1", "A1")
    a2, e2 = corrected_trade("T2", "A2")
    alerts = [
        a1,
        a2,
        alert("A3", trade="T3"),  # amendment left price unchanged
        alert("A4", trade="T4", atype="CANCEL_AMEND_PATTERN"),  # cancel + rebook
        alert("A5", trade="T5", atype="LATE_TRADE"),  # not provable from contract
        alert("A6", trade="T6"),  # lifecycle starts with AMEND
        alert("A7", trade="T7"),  # open RFI
        alert("A8", trade="T8", atype="UNKNOWN_TYPE"),
        alert("A9", trade="T9", text="   "),  # no explanation
    ]
    events = [
        *e1,
        *e2,
        event("T3", "NEW", 0, "100"),
        event("T3", "AMEND", 2, "100"),
        event("T4", "NEW", 0),
        event("T4", "CANCEL", 3),
        event("T4R", "NEW", 4),
        event("T5", "NEW", 0),
        event("T6", "AMEND", 0, "100"),
        event("T7", "NEW", 0, "100"),
        event("T7", "AMEND", 2, "101"),
        event("T8", "NEW", 0),
        event("T9", "NEW", 0, "100"),
        event("T9", "AMEND", 2, "101"),
    ]
    result = run(source(alerts, events, [rfi("A7", "OPENED", 5)]), cfg(), AS_OF)
    got = _by_trade(result)
    assert got == {
        "T1": (BULK, ()),
        "T2": (BULK, ()),
        "T3": (
            IND,
            (
                "CLAIM_CONTRADICTED:PRICE_CORRECTION",
                "EVIDENCE_MISSING:ALL_CLAIMS_VERIFIED",
            ),
        ),
        "T4": (IND, ("COHORT_BELOW_MIN_SIZE",)),
        "T5": (
            IND,
            (
                "CLAIM_NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS:LATE_BOOKING",
                "EVIDENCE_MISSING:ALL_CLAIMS_VERIFIED",
            ),
        ),
        "T6": (
            IND,
            (
                "LIFECYCLE:FIRST_EVENT_NOT_NEW",
                "CLAIM_NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS:PRICE_CORRECTION",
                "EVIDENCE_MISSING:TRADE_LIFECYCLE_VALID",
                "EVIDENCE_MISSING:ALL_CLAIMS_VERIFIED",
            ),
        ),
        "T7": (IND, ("OPEN_RFI",)),
        "T8": (IND, ("CATEGORY_UNMAPPED", "NO_VERIFIED_EXPLANATION")),
        "T9": (IND, ("EVIDENCE_MISSING:EXPLANATION_PRESENT",)),
    }
    assert len(result.cohorts) == 1
    cohort = result.cohorts[0]
    assert cohort.status == "PROPOSED_AWAITING_SUPERVISOR_ATTESTATION"
    assert {c.episode.trade_id for c in result.cases if c.case_id in cohort.case_ids} == {
        "T1",
        "T2",
    }
    assert "T5" in {c.episode.trade_id for c in result.cases if c.case_id in result.rfi_drafts}
    assert all(r.startswith("DRAFT - NOT SENT") for r in result.rfi_drafts.values())


def test_alerts_far_apart_form_separate_episodes() -> None:
    alerts = [alert("A1", h=1), alert("A2", h=2), alert("A3", h=150)]
    events = [event("T1", "NEW", 0), event("T1", "AMEND", 0.5, "101")]
    result = run(source(alerts, events), cfg(), AS_OF)
    assert sorted(c.episode.alert_ids for c in result.cases) == [("A1", "A2"), ("A3",)]


def test_closed_rfi_is_not_open() -> None:
    a, e = corrected_trade("T1", "A1")
    rfis = [rfi("A1", "OPENED", 3), rfi("A1", "CLOSED", 4)]
    result = run(source([a], e, rfis), cfg(), AS_OF)
    assert not result.cases[0].open_rfi


def test_cancel_rebook_quantity_mismatch_contradicts() -> None:
    alerts = [alert("A1", atype="CANCEL_AMEND_PATTERN")]
    events = [event("T1", "NEW", 0), event("T1", "CANCEL", 1), event("T2", "NEW", 2, qty="11")]
    case = run(source(alerts, events), cfg(), AS_OF).cases[0]
    assert "CLAIM_CONTRADICTED:CANCEL_REBOOK" in case.reasons
