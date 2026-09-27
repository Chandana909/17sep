"""Strict ingestion: versioned column mapping -> contract rows -> typed records.

Real SCP/CAL column names live only in a mapping TOML (docs/playbooks/integrate-real-data.md).
Unmapped columns fail loudly (schema drift), dirty values fail loudly, and naive timestamps
are rejected unless the mapping declares an offset or an IANA time zone.

Per entity, a mapping declares:
- `source` and optional `reader` / `reader_options` (csv, jsonl, parquet, xlsx)
- `columns` (source column -> contract field)
- `ignore` (source columns deliberately dropped)
- `values` (code translations per field)
- `constants` (a fixed value for a field the source lacks)
- `copy` (derive a field from another contract field, e.g. RECORD_TIME from EVENT_TIME)
- `timestamp_format` (strptime pattern)
- `naive_timestamp_offset` or `timezone`
- `decimal_comma`

`validate_source` reports **every** problem with the row, the field and a fix hint, so a
mapping can be iterated to green in a few passes. `load_source_bundle` is the strict loader.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from asas.core.errors import DataContractError, UnknownFieldError
from asas.data.contract import (
    DECIMAL_FIELDS,
    ENTITIES,
    ENTITY_FIELDS,
    ENUM_VALUES,
    PERSON_FIELDS,
    REQUIRED_FIELDS,
    TIMESTAMP_FIELDS,
    WORKFLOW_FIELDS,
)
from asas.data.readers import read_rows
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
_ENTITY_KEYS = frozenset(
    {
        "source",
        "reader",
        "reader_options",
        "columns",
        "ignore",
        "values",
        "constants",
        "copy",
        "timestamp_format",
        "naive_timestamp_offset",
        "timezone",
        "decimal_comma",
    }
)


@dataclass(frozen=True)
class SourceBundle:
    trade_events: tuple[TradeEvent, ...] = ()
    trade_persons: tuple[TradePerson, ...] = ()
    alerts: tuple[Alert, ...] = ()
    alert_annexes: tuple[AlertAnnex, ...] = ()
    rfi_events: tuple[RfiEvent, ...] = ()
    outcomes: tuple[ReviewOutcome, ...] = ()


# ---------------------------------------------------------------- mapping


def _timestamp(value: object, fmt: str | None, tz: tzinfo | None, name: str) -> object:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if fmt:
            try:
                value = datetime.strptime(text, fmt)
            except ValueError as exc:
                raise DataContractError(
                    f"{name}: {text!r} does not match timestamp_format {fmt!r}"
                ) from exc
        else:
            try:
                value = datetime.fromisoformat(text)
            except ValueError:
                return value  # parse_ts reports it with a hint
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if isinstance(value, datetime) and value.tzinfo is None and tz is not None:
        return value.replace(tzinfo=tz)
    return value


@dataclass(frozen=True)
class EntityMapping:
    entity: str
    source: str
    columns: Mapping[str, str]
    ignore: frozenset[str] = frozenset()
    values: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    tz: tzinfo | None = None
    constants: Mapping[str, str] = field(default_factory=dict)
    copy: Mapping[str, str] = field(default_factory=dict)
    timestamp_format: str | None = None
    decimal_comma: bool = False
    reader: str | None = None
    reader_options: Mapping[str, str] = field(default_factory=dict)

    def unknown_columns(self, header: Iterable[str]) -> list[str]:
        return sorted(k for k in header if k not in self.columns and k not in self.ignore)

    def to_contract(self, raw: Row) -> dict[str, object]:
        unknown = self.unknown_columns(raw)
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
            if target in TIMESTAMP_FIELDS:
                value = _timestamp(value, self.timestamp_format, self.tz, target)
            elif target in DECIMAL_FIELDS and self.decimal_comma and isinstance(value, str):
                value = value.replace(".", "").replace(",", ".")
            out[target] = value
        for target, constant in self.constants.items():
            if out.get(target) in (None, ""):
                out[target] = constant
        for target, origin in self.copy.items():
            if out.get(target) in (None, ""):
                out[target] = out.get(origin)
        return out


@dataclass(frozen=True)
class SourceMapping:
    version: str
    entities: Mapping[str, EntityMapping]


def _offset(text: str) -> timezone:
    sign = -1 if text.startswith("-") else 1
    try:
        hours, minutes = text.lstrip("+-").split(":")
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
    except ValueError as exc:
        raise DataContractError(f"naive_timestamp_offset must look like +00:00: {text!r}") from exc


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DataContractError(f"unknown timezone {name!r} (use an IANA name)") from exc


def _entity_mapping(name: str, spec: Mapping[str, Any]) -> EntityMapping:
    extra = sorted(set(spec) - _ENTITY_KEYS)
    if extra:
        raise DataContractError(f"{name}: unknown mapping keys {extra}")
    fields = ENTITY_FIELDS[name]
    columns = dict(spec.get("columns", {}))
    constants = {str(k): str(v) for k, v in spec.get("constants", {}).items()}
    copies = {str(k): str(v) for k, v in spec.get("copy", {}).items()}
    bad = sorted(
        (set(columns.values()) | set(constants) | set(copies) | set(copies.values())) - fields
    )
    if bad:
        raise DataContractError(f"{name}.columns targets fields not in the contract: {bad}")
    targets = list(columns.values())
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        raise DataContractError(f"{name}.columns maps several columns to {dupes}")
    missing = sorted(REQUIRED_FIELDS[name] - set(targets) - set(constants) - set(copies))
    if missing:
        raise DataContractError(f"{name}: required contract fields not mapped: {missing}")
    offset, zone = spec.get("naive_timestamp_offset"), spec.get("timezone")
    if offset and zone:
        raise DataContractError(f"{name}: set naive_timestamp_offset or timezone, not both")
    tz: tzinfo | None = None
    if isinstance(offset, str):
        tz = _offset(offset)
    elif isinstance(zone, str):
        tz = _zone(zone)
    fmt = spec.get("timestamp_format")
    return EntityMapping(
        entity=name,
        source=str(spec.get("source", "")),
        columns=columns,
        ignore=frozenset(spec.get("ignore", [])),
        values={
            k: {str(a): str(b) for a, b in v.items()} for k, v in spec.get("values", {}).items()
        },
        tz=tz,
        constants=constants,
        copy=copies,
        timestamp_format=str(fmt) if fmt else None,
        decimal_comma=bool(spec.get("decimal_comma", False)),
        reader=str(spec["reader"]) if spec.get("reader") else None,
        reader_options={str(k): str(v) for k, v in spec.get("reader_options", {}).items()},
    )


def mapping_from_dict(data: Mapping[str, Any]) -> SourceMapping:
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise DataContractError("mapping.version is required")
    unknown_sections = sorted(set(data) - {"version", *ENTITIES})
    if unknown_sections:
        raise DataContractError(f"unknown mapping sections: {unknown_sections}")
    entities = {name: _entity_mapping(name, data[name]) for name in ENTITIES if name in data}
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
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = parse_str(value, name)
    if not text.lstrip("-").isdigit():
        raise DataContractError(f"{name}: not an integer")
    return int(text)


def _enum(value: object, name: str, allowed: Iterable[str]) -> str:
    text = parse_str(value, name)
    if text not in set(allowed):
        raise DataContractError(f"{name}: unknown value {text!r}")
    return text


# ---------------------------------------------------------------- row parsers


def trade_event_from_row(row: Row) -> tuple[TradeEvent, TradePerson | None]:
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
    trader = parse_opt_str(row.get("TRADER_ID"), "TRADER_ID")
    person = (
        TradePerson(trade_id=event.trade_id, record_time=event.record_time, trader_id=trader)
        if trader and event.version == 1
        else None
    )
    return event, person


def alert_from_row(row: Row) -> tuple[Alert, AlertAnnex]:
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
        if version is not None and str(version).strip()
        else None,
        book=parse_str(row.get("BOOK"), "BOOK"),
        desk=parse_str(row.get("DESK"), "DESK"),
        instrument_id=parse_str(row.get("INSTRUMENT_ID"), "INSTRUMENT_ID"),
        explanation=text if isinstance(text, str) and text.strip() else None,
    )
    annex = AlertAnnex(
        alert_id=alert.alert_id,
        record_time=alert.record_time,
        workflow={k: str(row[k]) for k in sorted(row) if k in WORKFLOW_FIELDS and row[k]},
        persons={k: str(row[k]) for k in sorted(row) if k in PERSON_FIELDS and row[k]},
    )
    return alert, annex


def rfi_from_row(row: Row) -> RfiEvent:
    return RfiEvent(
        alert_id=parse_str(row.get("ALERT_ID"), "ALERT_ID"),
        action=RfiAction(_enum(row.get("RFI_ACTION"), "RFI_ACTION", RfiAction)),
        record_time=parse_ts(row.get("RECORD_TIME"), "RECORD_TIME"),
    )


def outcome_from_row(row: Row) -> ReviewOutcome:
    return ReviewOutcome(
        outcome_id=parse_str(row.get("OUTCOME_ID"), "OUTCOME_ID"),
        alert_id=parse_str(row.get("ALERT_ID"), "ALERT_ID"),
        label=OutcomeLabel(_enum(row.get("OUTCOME"), "OUTCOME", OutcomeLabel)),
        quality=LabelQuality(_enum(row.get("LABEL_QUALITY"), "LABEL_QUALITY", LabelQuality)),
        decided_at=parse_ts(row.get("DECIDED_AT"), "DECIDED_AT"),
        decided_by=parse_str(row.get("DECIDED_BY"), "DECIDED_BY"),
    )


# ---------------------------------------------------------------- batch parsers (strict)


def parse_trade_events(
    rows: Iterable[Row],
) -> tuple[tuple[TradeEvent, ...], tuple[TradePerson, ...]]:
    events: list[TradeEvent] = []
    persons: list[TradePerson] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        event, person = trade_event_from_row(row)
        key = (event.trade_id, event.version)
        if key in seen:
            raise DataContractError(f"duplicate trade version {key}")
        seen.add(key)
        events.append(event)
        if person is not None:
            persons.append(person)
    return tuple(events), tuple(persons)


def parse_alerts(rows: Iterable[Row]) -> tuple[tuple[Alert, ...], tuple[AlertAnnex, ...]]:
    alerts: list[Alert] = []
    annexes: list[AlertAnnex] = []
    seen: set[str] = set()
    for row in rows:
        alert, annex = alert_from_row(row)
        if alert.alert_id in seen:
            raise DataContractError(f"duplicate ALERT_ID {alert.alert_id}")
        seen.add(alert.alert_id)
        alerts.append(alert)
        annexes.append(annex)
    return tuple(alerts), tuple(annexes)


def parse_rfi_events(rows: Iterable[Row]) -> tuple[RfiEvent, ...]:
    return tuple(rfi_from_row(r) for r in rows)


def parse_outcomes(rows: Iterable[Row]) -> tuple[ReviewOutcome, ...]:
    return tuple(outcome_from_row(r) for r in rows)


# ---------------------------------------------------------------- validation (collect all)


@dataclass
class RowIssue:
    """One distinct problem (same field and message), with how many rows show it."""

    entity: str
    column: str  # the contract field concerned, when the message names one
    message: str
    hint: str
    rows: list[int] = field(default_factory=list)  # first few 1-based data rows
    count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity,
            "field": self.column,
            "message": self.message,
            "hint": self.hint,
            "rows": self.rows,
            "count": self.count,
        }


@dataclass
class EntityReport:
    entity: str
    source: str
    rows: int = 0
    parsed: int = 0
    failed: int = 0
    unknown_columns: list[str] = field(default_factory=list)
    unmapped_columns_in_mapping: list[str] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not (self.failed or self.unknown_columns or self.error)


@dataclass
class ValidationReport:
    mapping_version: str
    entities: dict[str, EntityReport]

    @property
    def ok(self) -> bool:
        return all(e.ok for e in self.entities.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "mapping_version": self.mapping_version,
            "ok": self.ok,
            "entities": {
                name: {
                    "source": e.source,
                    "rows": e.rows,
                    "parsed": e.parsed,
                    "failed": e.failed,
                    "unknown_columns": e.unknown_columns,
                    "mapped_columns_absent_from_file": e.unmapped_columns_in_mapping,
                    "error": e.error,
                    "issues": [i.to_dict() for i in e.issues],
                }
                for name, e in self.entities.items()
            },
        }

    def render(self) -> str:
        lines = [f"mapping {self.mapping_version}: {'OK' if self.ok else 'FAILED'}"]
        for name, e in self.entities.items():
            status = "ok" if e.ok else "FAILED"
            lines.append(f"- {name} ({e.source}): {e.parsed}/{e.rows} rows parsed, {status}")
            if e.error:
                lines.append(f"    error: {e.error}")
            if e.unknown_columns:
                lines.append(
                    f"    unknown columns {e.unknown_columns}: map them in [{name}.columns] "
                    f"or list them in {name}.ignore"
                )
            if e.unmapped_columns_in_mapping:
                lines.append(
                    f"    mapped but absent from the file: {e.unmapped_columns_in_mapping}"
                )
            for i in e.issues:
                where = ", ".join(str(r) for r in i.rows)
                lines.append(f"    {i.message} ({i.count} rows, e.g. rows {where})")
                lines.append(f"      FIX: {i.hint}")
        return "\n".join(lines)


def _generalise(message: str) -> str:
    """Group messages that differ only by the offending key (duplicates)."""
    return "duplicate key" if message.startswith("duplicate key") else message


def _field_of(message: str) -> str:
    head = message.split(":", 1)[0].strip()
    return head if head.isupper() or "_" in head else ""


def hint_for(entity: str, message: str) -> str:
    """A concrete next step for a validation message (for people and for coding models)."""
    name = _field_of(message)
    if "naive timestamp" in message:
        return f'set naive_timestamp_offset = "+00:00" or timezone = "Europe/London" in [{entity}]'
    if "timestamp_format" in message:
        return f"fix timestamp_format in [{entity}] (Python strptime codes, e.g. %d/%m/%Y %H:%M)"
    if "not an ISO timestamp" in message:
        return f'set timestamp_format in [{entity}], e.g. "%d/%m/%Y %H:%M:%S"'
    if "unknown value" in message and name in ENUM_VALUES:
        allowed = ", ".join(sorted(ENUM_VALUES[name]))
        return f"translate the code in [{entity}.values.{name}] to one of {allowed}"
    if "required non-empty" in message or "missing timestamp" in message:
        return f"map a source column to {name} in [{entity}.columns] or set [{entity}.constants]"
    if "not a decimal" in message:
        return f"clean {name} at source, or set decimal_comma = true in [{entity}] for 1.234,56"
    if "not an integer" in message:
        return f"{name} must be a whole number; check the source column mapped to it"
    if "duplicate" in message:
        return "the key repeats in the extract; deduplicate or check the key column mapping"
    if "unknown fields" in message:
        return f"map the column in [{entity}.columns] or add it to {entity}.ignore"
    return "see docs/field-contract.md for the expected value"


_ROW_PARSERS: Mapping[str, Callable[[Row], object]] = {
    "trade_events": trade_event_from_row,
    "alerts": alert_from_row,
    "rfi_events": rfi_from_row,
    "outcomes": outcome_from_row,
}


def _key(entity: str, parsed: object) -> str | None:
    if entity == "trade_events" and isinstance(parsed, tuple):
        event = parsed[0]
        return f"{event.trade_id}#{event.version}"
    if entity == "alerts" and isinstance(parsed, tuple):
        return str(parsed[0].alert_id)
    if entity == "outcomes" and isinstance(parsed, ReviewOutcome):
        return parsed.outcome_id
    return None


def process_source(
    data_dir: str | Path, mapping: SourceMapping, max_issues: int = 50
) -> tuple[ValidationReport, SourceBundle]:
    """Read, map and parse every entity, collecting every problem instead of stopping."""
    root = Path(data_dir)
    reports: dict[str, EntityReport] = {}
    parsed: dict[str, list[object]] = defaultdict(list)
    for name in ENTITIES:
        em = mapping.entities.get(name)
        if em is None:
            continue
        report = EntityReport(name, em.source)
        reports[name] = report
        try:
            raw_rows = read_rows(root / em.source, em.reader, em.reader_options)
        except DataContractError as exc:
            report.error = str(exc)
            continue
        header = sorted({k for r in raw_rows for k in r})
        report.unknown_columns = em.unknown_columns(header)
        report.unmapped_columns_in_mapping = sorted(set(em.columns) - set(header))
        seen: set[str] = set()
        distinct: dict[str, RowIssue] = {}
        for number, raw in enumerate(raw_rows, start=1):
            report.rows += 1
            try:
                # unknown columns are reported once for the entity, not on every row
                known = {k: v for k, v in raw.items() if k in em.columns or k in em.ignore}
                row = em.to_contract(known)
                value = _ROW_PARSERS[name](row)
                key = _key(name, value)
                if key is not None and key in seen:
                    raise DataContractError(f"duplicate key {key}")
                if key is not None:
                    seen.add(key)
            except (DataContractError, ValueError) as exc:
                report.failed += 1
                message = _generalise(str(exc))
                issue = distinct.get(message)
                if issue is None and len(distinct) < max_issues:
                    issue = RowIssue(name, _field_of(message), message, hint_for(name, message))
                    distinct[message] = issue
                    report.issues.append(issue)
                if issue is not None:
                    issue.count += 1
                    if len(issue.rows) < 3:
                        issue.rows.append(number)
                continue
            report.parsed += 1
            parsed[name].append(value)
    events = [v for v in parsed["trade_events"] if isinstance(v, tuple)]
    alerts = [v for v in parsed["alerts"] if isinstance(v, tuple)]
    bundle = SourceBundle(
        trade_events=tuple(e for e, _ in events),
        trade_persons=tuple(p for _, p in events if p is not None),
        alerts=tuple(a for a, _ in alerts),
        alert_annexes=tuple(x for _, x in alerts),
        rfi_events=tuple(v for v in parsed["rfi_events"] if isinstance(v, RfiEvent)),
        outcomes=tuple(v for v in parsed["outcomes"] if isinstance(v, ReviewOutcome)),
    )
    return ValidationReport(mapping.version, reports), bundle


def validate_source(
    data_dir: str | Path, mapping: SourceMapping, max_issues: int = 50
) -> ValidationReport:
    return process_source(data_dir, mapping, max_issues)[0]


def load_source_bundle(data_dir: str | Path, mapping: SourceMapping) -> SourceBundle:
    """Strict: any failing row, unknown column or unreadable file refuses the whole load."""
    report, bundle = process_source(data_dir, mapping, max_issues=5)
    if not report.ok:
        raise DataContractError(
            "source failed validation; run `python -m asas data check` for every issue:\n"
            + report.render()
        )
    return bundle


def load_csv_bundle(data_dir: str | Path, mapping: SourceMapping) -> SourceBundle:
    """Backwards-compatible name for `load_source_bundle`."""
    return load_source_bundle(data_dir, mapping)
