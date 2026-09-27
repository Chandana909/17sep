"""Data integration report: what ASAS can and cannot do with the data you actually have.

Given a parsed source bundle, the capability matrix lists per hypothesis, deviation signal,
production rule and platform feature whether it is ENABLED or DEGRADED, and why. It uses the
same `DataCapabilities` the engine uses at run time, so the report and the behaviour agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from asas.core.config import Config
from asas.data.ingest import SourceBundle
from asas.domain.models import RuleSpec
from asas.engine.capabilities import SIGNAL_FIELDS, detect, rule_gaps
from asas.engine.hypotheses import CATALOG

ENABLED = "ENABLED"


def capability_matrix(
    bundle: SourceBundle, cfg: Config, rules: Sequence[RuleSpec]
) -> dict[str, Any]:
    caps = detect(bundle, cfg)
    hypotheses = {}
    for h in CATALOG:
        missing = sorted(h.fields & caps.unavailable)
        hypotheses[h.type] = (
            f"DEGRADED: needs {', '.join(missing)} (always INSUFFICIENT, abstains)"
            if missing
            else ENABLED
        )
    signals = {}
    for name in cfg.strings("anomaly", "signals"):
        missing = sorted(caps.missing_for_signal(name))
        signals[name] = f"UNAVAILABLE: needs {', '.join(missing)}" if missing else ENABLED
    events = bundle.trade_events
    links = {
        "ORIGINAL_TRADE_ID": sum(1 for e in events if e.original_trade_id),
        "ALTERNATE_TRADE_ID": sum(1 for e in events if e.alternate_trade_id),
        "URN_REF": sum(1 for e in events if e.urn_ref),
    }
    entities = caps.entities
    features = {
        "episode_linking_strong": ENABLED
        if links["ORIGINAL_TRADE_ID"]
        else "DEGRADED: no ORIGINAL_TRADE_ID values; rebooks rely on agent-proposed links",
        "episode_linking_medium": ENABLED
        if links["URN_REF"] or links["ALTERNATE_TRADE_ID"]
        else "DEGRADED: no URN_REF / ALTERNATE_TRADE_ID values",
        "curated_labels": ENABLED
        if entities["curated_outcomes"]
        else "DEGRADED: no CURATED outcomes; replay, counterexample and discovery gates fail",
        "open_rfi_override": ENABLED if entities["rfi_events"] else "DEGRADED: no RFI events",
        "trader_baseline": ENABLED
        if entities["trade_persons"]
        else "DEGRADED: no TRADER_ID; the entitled trader baseline tool returns nothing",
        "alert_explanations": ENABLED
        if any(a.explanation for a in bundle.alerts)
        else "DEGRADED: no EXPLANATION_TEXT (context only; decisions never read it)",
    }
    return {
        "coverage": {k: str(v) for k, v in sorted(caps.coverage.items())},
        "unavailable_fields": sorted(caps.unavailable),
        "entities": dict(entities),
        "link_key_rows": links,
        "hypotheses": hypotheses,
        "deviation_signals": signals,
        "signal_fields": {k: sorted(v) for k, v in SIGNAL_FIELDS.items()},
        "rule_gaps": list(rule_gaps(rules, caps)),
        "features": features,
    }


def render_matrix(matrix: dict[str, Any]) -> str:
    lines = ["Data capabilities"]
    lines.append(f"  measure-field coverage: {matrix['coverage']}")
    lines.append(f"  unavailable fields: {matrix['unavailable_fields'] or 'none'}")
    lines.append(f"  rows: {matrix['entities']}")
    for section in ("hypotheses", "deviation_signals", "features"):
        lines.append(f"  {section}:")
        for name, status in matrix[section].items():
            lines.append(f"    {name:<32} {status}")
    gaps = matrix["rule_gaps"]
    lines.append(f"  production rules that cannot fire: {', '.join(gaps) if gaps else 'none'}")
    return "\n".join(lines)
