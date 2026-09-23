"""Parse raw source rows into typed records. Strict: dirty data fails loudly."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from asas.fields import (
    ALERT_FIELDS,
    PAST_CASE_FIELDS,
    PERSON_FIELDS,
    RFI_EVENT_FIELDS,
    TRADE_EVENT_FIELDS,
    WORKFLOW_FIELDS,
    DataContractError,
    normalize_row,
)
from asas.models import Alert, AlertAnnex, EventType, PastCase, RfiAction, RfiEvent, TradeEvent

Row = Mapping[str, object]


def parse_ts(value: object, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DataContractError(f"{field}: not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise DataContractError(f"{field}: missing or not a timestamp")
    if value.tzinfo is None:
        raise DataContractError(f"{field}: naive timestamp rejected (timezone required)")
    return value.astimezone(UTC)


def parse_decimal(value: object, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise DataContractError(f"{field}: not a decimal")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DataContractError(f"{field}: not a decimal") from exc
    if not result.is_finite():
        raise DataContractError(f"{field}: non-finite decimal rejected")
    return result


def parse_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataContractError(f"{field}: required non-empty string")
    return value.strip()


def _free_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DataContractError(f"{field}: free text must be a string")
    return value


def _pairs(row: Row, names: frozenset[str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, "" if row[k] is None else str(row[k])) for k in row if k in names))


def parse_alerts(rows: Iterable[Row]) -> tuple[tuple[Alert, ...], tuple[AlertAnnex, ...]]:
    alerts: list[Alert] = []
    annexes: list[AlertAnnex] = []
    seen: set[str] = set()
    for raw in rows:
        row = normalize_row(raw, ALERT_FIELDS)
        alert = Alert(
            alert_id=parse_str(row.get("ALERT_ID"), "ALERT_ID"),
            alert_type=parse_str(row.get("ALERT_TYPE"), "ALERT_TYPE"),
            trade_id=parse_str(row.get("TRADE_ID"), "TRADE_ID"),
            instrument_id=parse_str(row.get("INSTRUMENT_ID"), "INSTRUMENT_ID"),
            book_id=parse_str(row.get("BOOK_ID"), "BOOK_ID"),
            alert_time=parse_ts(row.get("ALERT_TIME"), "ALERT_TIME"),
            record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
            explanation_text=_free_text(row.get("EXPLANATION_TEXT"), "EXPLANATION_TEXT"),
        )
        if alert.alert_id in seen:
            raise DataContractError(f"duplicate ALERT_ID: {alert.alert_id}")
        seen.add(alert.alert_id)
        alerts.append(alert)
        annexes.append(
            AlertAnnex(
                alert_id=alert.alert_id,
                record_time=alert.record_time,
                workflow=_pairs(row, WORKFLOW_FIELDS),
                persons=_pairs(row, PERSON_FIELDS),
            )
        )
    return tuple(alerts), tuple(annexes)


def parse_trade_events(rows: Iterable[Row]) -> tuple[TradeEvent, ...]:
    out: list[TradeEvent] = []
    for raw in rows:
        row = normalize_row(raw, TRADE_EVENT_FIELDS)
        etype = parse_str(row.get("EVENT_TYPE"), "EVENT_TYPE")
        if etype not in EventType.__members__:
            raise DataContractError(f"EVENT_TYPE: unknown value {etype!r}")
        out.append(
            TradeEvent(
                trade_id=parse_str(row.get("TRADE_ID"), "TRADE_ID"),
                event_type=EventType(etype),
                event_time=parse_ts(row.get("EVENT_TIME"), "EVENT_TIME"),
                record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
                instrument_id=parse_str(row.get("INSTRUMENT_ID"), "INSTRUMENT_ID"),
                book_id=parse_str(row.get("BOOK_ID"), "BOOK_ID"),
                price=parse_decimal(row.get("PRICE"), "PRICE"),
                quantity=parse_decimal(row.get("QUANTITY"), "QUANTITY"),
            )
        )
    return tuple(out)


def parse_rfi_events(rows: Iterable[Row]) -> tuple[RfiEvent, ...]:
    out: list[RfiEvent] = []
    for raw in rows:
        row = normalize_row(raw, RFI_EVENT_FIELDS)
        action = parse_str(row.get("RFI_ACTION"), "RFI_ACTION")
        if action not in RfiAction.__members__:
            raise DataContractError(f"RFI_ACTION: unknown value {action!r}")
        out.append(
            RfiEvent(
                alert_id=parse_str(row.get("ALERT_ID"), "ALERT_ID"),
                action=RfiAction(action),
                record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
            )
        )
    return tuple(out)


def parse_past_cases(rows: Iterable[Row]) -> tuple[PastCase, ...]:
    out: list[PastCase] = []
    for raw in rows:
        row = normalize_row(raw, PAST_CASE_FIELDS)
        out.append(
            PastCase(
                case_id=parse_str(row.get("CASE_ID"), "CASE_ID"),
                category=parse_str(row.get("CATEGORY"), "CATEGORY"),
                outcome=parse_str(row.get("OUTCOME"), "OUTCOME"),
                decided_at=parse_ts(row.get("DECIDED_AT"), "DECIDED_AT"),
            )
        )
    return tuple(out)
