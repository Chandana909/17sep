"""Data capabilities: which contract fields the loaded data actually supports.

ASAS is loosely coupled to its data. A deployment may lack a field entirely (no PRICE feed) or
the business may declare one unreliable. Every feature that needs a field names it here, and
when the field is unavailable the feature degrades to INSUFFICIENT (hypotheses), is skipped
with a note (deviation signals), or is reported as a coverage gap (rules). Nothing silently
reads a missing field as "no problem" (rail 5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from asas.core.config import Config
from asas.data.ingest import SourceBundle
from asas.domain.models import LabelQuality, RuleSpec, TradeEvent

# Contract field -> TradeEvent attribute for the measure fields whose coverage is profiled.
_EVENT_ATTR: Mapping[str, str] = {
    "SIDE": "side",
    "QUANTITY": "quantity",
    "PRICE": "price",
    "NOTIONAL_USD": "notional_usd",
}

# Signals that read an optional measure field. Signals not listed use required fields only.
SIGNAL_FIELDS: Mapping[str, frozenset[str]] = {
    "max_price_change_pct": frozenset({"PRICE"}),
    "rebook_price_change_pct": frozenset({"PRICE"}),
    "price_round_trip": frozenset({"PRICE"}),
    "max_qty_change_pct": frozenset({"QUANTITY"}),
    "notional_usd_max": frozenset({"NOTIONAL_USD"}),
}


@dataclass(frozen=True)
class DataCapabilities:
    coverage: Mapping[str, Decimal]  # share of trade events carrying each measure field
    unavailable: frozenset[str]  # measure fields below coverage or declared unreliable
    entities: Mapping[str, int]  # row counts per source entity (and curated labels)

    def missing_for_signal(self, signal: str) -> frozenset[str]:
        return SIGNAL_FIELDS.get(signal, frozenset()) & self.unavailable

    def signal_available(self, signal: str) -> bool:
        return not self.missing_for_signal(signal)

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage": {k: str(v) for k, v in sorted(self.coverage.items())},
            "unavailable": sorted(self.unavailable),
            "entities": dict(sorted(self.entities.items())),
        }


def _coverage(events: Iterable[TradeEvent], fields: Iterable[str]) -> dict[str, Decimal]:
    rows = list(events)
    out: dict[str, Decimal] = {}
    for f in fields:
        attr = _EVENT_ATTR.get(f)
        if attr is None or not rows:
            out[f] = Decimal(0)
            continue
        present = sum(1 for e in rows if getattr(e, attr) is not None)
        out[f] = (Decimal(present) / Decimal(len(rows))).quantize(Decimal("0.0001"))
    return out


def detect(bundle: SourceBundle, cfg: Config) -> DataCapabilities:
    fields = cfg.strings("data", "measure_fields")
    coverage = _coverage(bundle.trade_events, fields)
    minimum = cfg.decimal("data", "min_field_coverage")
    declared = set(cfg.strings("data", "unavailable_fields"))
    unavailable = frozenset({f for f, c in coverage.items() if c < minimum} | declared)
    entities = {
        "trade_events": len(bundle.trade_events),
        "alerts": len(bundle.alerts),
        "rfi_events": len(bundle.rfi_events),
        "outcomes": len(bundle.outcomes),
        "curated_outcomes": sum(o.quality is LabelQuality.CURATED for o in bundle.outcomes),
        "trade_persons": len(bundle.trade_persons),
    }
    return DataCapabilities(coverage, unavailable, entities)


def rule_gaps(rules: Iterable[RuleSpec], caps: DataCapabilities) -> tuple[str, ...]:
    """Production subrules that cannot fire because a field they need is unavailable."""
    gaps: list[str] = []
    for rule in rules:
        missing = sorted({f for c in rule.conditions for f in caps.missing_for_signal(c.signal)})
        if missing:
            gaps.append(f"{rule.subrule_id}:{'+'.join(missing)}")
    return tuple(sorted(gaps))
