"""Strict ingestion: versioned column mapping -> contract rows -> typed records.

Real SCP/CAL column names live only in a mapping TOML (see docs/integration.md). Unmapped
columns fail loudly (schema drift); dirty values fail loudly; naive timestamps are rejected
unless the mapping declares an offset."""

from __future__ import annotations

import csv
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from asas.core.errors import DataContractError, UnknownFieldError
from asas.data.contract import (
    ENTITIES,
    ENTITY_FIELDS,
    PERSON_FIELDS,
    REQUIRED_FIELDS,
    TIMESTAMP_FIELDS,
    WORKFLOW_FIELDS,
)
from asas.domain.models import (
    Alert,
    AlertAnnex,
    EventType,
    LabelQuality,
    OutcomeLabel,
    ReviewOutcome,
    RfiAction,
    RfiEvent,
    Side,
    TradeEvent,
    TradePerson,
)

Row = Mapping[str, object]


@dataclass(frozen=True)
class SourceBundle:
    trade_events: tuple[TradeEvent, ...] = ()
    trade_persons: tuple[TradePerson, ...] = ()
    alerts: tuple[Alert, ...] = ()
    alert_annexes: tuple[AlertAnnex, ...] = ()
    rfi_events: tuple[RfiEvent, ...] = ()
    outcomes: tuple[ReviewOutcome, ...] = ()


# ---------------------------------------------------------------- mapping


@dataclass(frozen=True)
class EntityMapping:
    entity: str
    source: str
    columns: Mapping[str, str]
    ignore: frozenset[str] = frozenset()
    values: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    tz: timezone | None = None

    def to_contract(self, raw: Row) -> dict[str, object]:
        unknown = [k for k in raw if k not in self.columns and k not in self.ignore]
        if unknown:
            raise UnknownFieldError(unknown)
        out: dict[str, object] = {}
        for src, target in self.columns.items():
            if src not in raw:
                continue
            value = raw[src]
            vmap = self.values.get(target)
            if vmap is not None and isinstance(value, str) and value.strip() in vmap:
                value = vmap[value.strip()]
            if target in TIMESTAMP_FIELDS and self.tz is not None:
                value = _attach_tz(value, self.tz)
            out[target] = value
        return out


@dataclass(frozen=True)
class SourceMapping:
    version: str
    entities: Mapping[str, EntityMapping]


def _attach_tz(value: object, tz: timezone) -> object:
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return value
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed
    return value


def _offset(text: str) -> timezone:
    sign = -1 if text.startswith("-") else 1
    try:
        hours, minutes = text.lstrip("+-").split(":")
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
    except ValueError as exc:
        raise DataContractError(f"naive_timestamp_offset must look like +00:00: {text!r}") from exc


def mapping_from_dict(data: Mapping[str, Any]) -> SourceMapping:
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise DataContractError("mapping.version is required")
    unknown_sections = sorted(set(data) - {"version", *ENTITIES})
    if unknown_sections:
        raise DataContractError(f"unknown mapping sections: {unknown_sections}")
    entities: dict[str, EntityMapping] = {}
    for name in ENTITIES:
        spec = data.get(name)
        if spec is None:
            continue
        columns = spec.get("columns", {})
        bad = sorted(set(columns.values()) - ENTITY_FIELDS[name])
        if bad:
            raise DataContractError(f"{name}.columns targets fields not in the contract: {bad}")
        targets = list(columns.values())
        dupes = sorted({t for t in targets if targets.count(t) > 1})
        if dupes:
            raise DataContractError(f"{name}.columns maps several columns to {dupes}")
        missing = sorted(REQUIRED_FIELDS[name] - set(targets))
        if missing:
            raise DataContractError(f"{name}: required contract fields not mapped: {missing}")
        offset = spec.get("naive_timestamp_offset")
        entities[name] = EntityMapping(
            entity=name,
            source=str(spec.get("source", "")),
            columns=dict(columns),
            ignore=frozenset(spec.get("ignore", [])),
            values={k: dict(v) for k, v in spec.get("values", {}).items()},
            tz=_offset(offset) if isinstance(offset, str) else None,
        )
    if "trade_events" not in entities or "alerts" not in entities:
        raise DataContractError("mapping must define trade_events and alerts")
    return SourceMapping(version, entities)


def load_mapping(path: str | Path) -> SourceMapping:
    with open(path, "rb") as fh:
        return mapping_from_dict(tomllib.load(fh))


def identity_mapping() -> SourceMapping:
    """Mapping for files already in contract column names (synthetic data, exports)."""
    return SourceMapping(
        "identity-1",
        {
            name: EntityMapping(name, f"{name}.csv", {f: f for f in ENTITY_FIELDS[name]})
            for name in ENTITIES
        },
    )


# ---------------------------------------------------------------- value parsing


def parse_ts(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise DataContractError(f"{name}: not an ISO timestamp") from exc
    if not isinstance(value, datetime):
        raise DataContractError(f"{name}: missing timestamp")
    if value.tzinfo is None:
        raise DataContractError(f"{name}: naive timestamp rejected (timezone required)")
    return value.astimezone(UTC)


def parse_decimal(value: object, name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise DataContractError(f"{name}: not a decimal")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DataContractError(f"{name}: not a decimal") from exc
    if not result.is_finite():
        raise DataContractError(f"{name}: non-finite decimal rejected")
    return result


def parse_str(value: object, name: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise DataContractError(f"{name}: required non-empty string")
    return value.strip()


def parse_opt_str(value: object, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_str(value, name)


def parse_int(value: object, name: str) -> int:
    text = parse_str(value, name)
    if not text.lstrip("-").isdigit():
        raise DataContractError(f"{name}: not an integer")
    return int(text)


def _enum(value: object, name: str, allowed: Iterable[str]) -> str:
    text = parse_str(value, name)
    if text not in set(allowed):
        raise DataContractError(f"{name}: unknown value {text!r}")
    return text


# ---------------------------------------------------------------- entity parsers


def parse_trade_events(
    rows: Iterable[Row],
) -> tuple[tuple[TradeEvent, ...], tuple[TradePerson, ...]]:
    events: list[TradeEvent] = []
    persons: list[TradePerson] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        side = parse_opt_str(row.get("SIDE"), "SIDE")
        event = TradeEvent(
            trade_id=parse_str(row.get("TRADE_ID"), "TRADE_ID"),
            version=parse_int(row.get("TRADE_VERSION"), "TRADE_VERSION"),
            event_type=EventType(_enum(row.get("EVENT_TYPE"), "EVENT_TYPE", EventType)),
            event_time=parse_ts(row.get("EVENT_TIME"), "EVENT_TIME"),
            record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
            book=parse_str(row.get("BOOK"), "BOOK"),
            desk=parse_str(row.get("DESK"), "DESK"),
            instrument_id=parse_str(row.get("INSTRUMENT_ID"), "INSTRUMENT_ID"),
            product_type=parse_str(row.get("PRODUCT_TYPE"), "PRODUCT_TYPE"),
            side=Side(_enum(side, "SIDE", Side)) if side else None,
            quantity=parse_decimal(row.get("QUANTITY"), "QUANTITY"),
            price=parse_decimal(row.get("PRICE"), "PRICE"),
            currency=parse_str(row.get("CURRENCY"), "CURRENCY"),
            notional_usd=parse_decimal(row.get("NOTIONAL_USD"), "NOTIONAL_USD"),
            original_trade_id=parse_opt_str(row.get("ORIGINAL_TRADE_ID"), "ORIGINAL_TRADE_ID"),
            alternate_trade_id=parse_opt_str(row.get("ALTERNATE_TRADE_ID"), "ALTERNATE_TRADE_ID"),
            urn_ref=parse_opt_str(row.get("URN_REF"), "URN_REF"),
            source=parse_str(row.get("SOURCE"), "SOURCE"),
        )
        key = (event.trade_id, event.version)
        if key in seen:
            raise DataContractError(f"duplicate trade version {key}")
        seen.add(key)
        events.append(event)
        trader = parse_opt_str(row.get("TRADER_ID"), "TRADER_ID")
        if trader and event.version == 1:
            persons.append(
                TradePerson(
                    trade_id=event.trade_id, record_time=event.record_time, trader_id=trader
                )
            )
    return tuple(events), tuple(persons)


def parse_alerts(rows: Iterable[Row]) -> tuple[tuple[Alert, ...], tuple[AlertAnnex, ...]]:
    alerts: list[Alert] = []
    annexes: list[AlertAnnex] = []
    seen: set[str] = set()
    for row in rows:
        text = row.get("EXPLANATION_TEXT")
        if text is not None and not isinstance(text, str):
            raise DataContractError("EXPLANATION_TEXT: free text must be a string")
        version = row.get("TRADE_VERSION")
        alert = Alert(
            alert_id=parse_str(row.get("ALERT_ID"), "ALERT_ID"),
            rule_id=parse_str(row.get("RULE_ID"), "RULE_ID"),
            subrule_id=parse_str(row.get("SUBRULE_ID"), "SUBRULE_ID"),
            alert_time=parse_ts(row.get("ALERT_TIME"), "ALERT_TIME"),
            record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
            trade_id=parse_str(row.get("TRADE_ID"), "TRADE_ID"),
            trade_version=parse_int(version, "TRADE_VERSION")
            if parse_opt_str(version, "v")
            else None,
            book=parse_str(row.get("BOOK"), "BOOK"),
            desk=parse_str(row.get("DESK"), "DESK"),
            instrument_id=parse_str(row.get("INSTRUMENT_ID"), "INSTRUMENT_ID"),
            explanation=text if isinstance(text, str) and text.strip() else None,
        )
        if alert.alert_id in seen:
            raise DataContractError(f"duplicate ALERT_ID {alert.alert_id}")
        seen.add(alert.alert_id)
        alerts.append(alert)
        annexes.append(
            AlertAnnex(
                alert_id=alert.alert_id,
                record_time=alert.record_time,
                workflow={k: str(row[k]) for k in sorted(row) if k in WORKFLOW_FIELDS and row[k]},
                persons={k: str(row[k]) for k in sorted(row) if k in PERSON_FIELDS and row[k]},
            )
        )
    return tuple(alerts), tuple(annexes)


def parse_rfi_events(rows: Iterable[Row]) -> tuple[RfiEvent, ...]:
    return tuple(
        RfiEvent(
            alert_id=parse_str(r.get("ALERT_ID"), "ALERT_ID"),
            action=RfiAction(_enum(r.get("RFI_ACTION"), "RFI_ACTION", RfiAction)),
            record_time=parse_ts(r.get("RECORD_TIME"), "RECORD_TIME"),
        )
        for r in rows
    )


def parse_outcomes(rows: Iterable[Row]) -> tuple[ReviewOutcome, ...]:
    return tuple(
        ReviewOutcome(
            outcome_id=parse_str(r.get("OUTCOME_ID"), "OUTCOME_ID"),
            alert_id=parse_str(r.get("ALERT_ID"), "ALERT_ID"),
            label=OutcomeLabel(_enum(r.get("OUTCOME"), "OUTCOME", OutcomeLabel)),
            quality=LabelQuality(_enum(r.get("LABEL_QUALITY"), "LABEL_QUALITY", LabelQuality)),
            decided_at=parse_ts(r.get("DECIDED_AT"), "DECIDED_AT"),
            decided_by=parse_str(r.get("DECIDED_BY"), "DECIDED_BY"),
        )
        for r in rows
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise DataContractError(f"source file not found: {path}")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_csv_bundle(data_dir: str | Path, mapping: SourceMapping) -> SourceBundle:
    root = Path(data_dir)

    def rows(entity: str) -> list[dict[str, object]]:
        em = mapping.entities.get(entity)
        if em is None:
            return []
        return [em.to_contract(r) for r in _read_csv(root / em.source)]

    events, persons = parse_trade_events(rows("trade_events"))
    alerts, annexes = parse_alerts(rows("alerts"))
    return SourceBundle(
        trade_events=events,
        trade_persons=persons,
        alerts=alerts,
        alert_annexes=annexes,
        rfi_events=parse_rfi_events(rows("rfi_events")),
        outcomes=parse_outcomes(rows("outcomes")),
    )
