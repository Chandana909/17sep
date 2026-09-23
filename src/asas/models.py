"""Typed, immutable domain records. Decision models carry no workflow or person fields;
those live only in AlertAnnex (structural enforcement of rails 8 and 11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class EventType(StrEnum):
    NEW = "NEW"
    AMEND = "AMEND"
    CANCEL = "CANCEL"


class RfiAction(StrEnum):
    OPENED = "OPENED"
    CLOSED = "CLOSED"


class Verdict(StrEnum):
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS"


class Treatment(StrEnum):
    BULK_CANDIDATE = "BULK_CANDIDATE"
    INDIVIDUAL_REVIEW = "INDIVIDUAL_REVIEW"


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    alert_type: str
    trade_id: str
    instrument_id: str
    book_id: str
    alert_time: datetime
    record_time: datetime
    explanation_text: str | None  # untrusted free text


@dataclass(frozen=True, slots=True)
class AlertAnnex:
    """Quarantined fields for reports only."""

    alert_id: str
    record_time: datetime
    workflow: tuple[tuple[str, str], ...]
    persons: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TradeEvent:
    trade_id: str
    event_type: EventType
    event_time: datetime
    record_time: datetime
    instrument_id: str
    book_id: str
    price: Decimal | None
    quantity: Decimal | None


@dataclass(frozen=True, slots=True)
class RfiEvent:
    alert_id: str
    action: RfiAction
    record_time: datetime


@dataclass(frozen=True, slots=True)
class PastCase:
    case_id: str
    category: str
    outcome: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claim: str
    verdict: Verdict
    basis: str


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    trade_id: str
    alert_ids: tuple[str, ...]
    start: datetime
    end: datetime
    events: tuple[TradeEvent, ...]
    lifecycle_issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewCase:
    case_id: str
    episode: Episode
    category: str | None
    claims: tuple[ClaimResult, ...]
    evidence_missing: tuple[str, ...]
    open_rfi: bool
    history_comparable: int
    history_adverse: int
    treatment: Treatment
    reasons: tuple[str, ...]
    priority: Decimal | None


COHORT_STATUS = "PROPOSED_AWAITING_SUPERVISOR_ATTESTATION"


@dataclass(frozen=True, slots=True)
class CohortProposal:
    cohort_id: str
    category: str
    signature: tuple[str, ...]
    case_ids: tuple[str, ...]
    status: str = COHORT_STATUS
