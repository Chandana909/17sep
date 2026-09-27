"""Deterministic case classification: what it is, how severe, how sure, and why.

- **Category:** a verified typology (TYPED_ANOMALY), an unexplained verified deviation, a
  verified benign explanation, or UNRESOLVED (abstained).
- **Severity:** comes from `classification.severity[<typology>]`, raised one level when the
  episode is above `scoring.materiality_usd` and `classification.materiality_bump` is set.
- **Confidence:** counts independent lines of evidence (corroboration) against
  `classification.confidence_high` and `confidence_medium`. Every line is named, so the
  confidence is auditable.

Anomaly lines of evidence:
- a typed hypothesis is verified
- a verified peer deviation on a signal the typology concerns (or any unexplained one)
- confirmation at every peer level
- a book-history deviation
- a rare combination
- a production rule detection
- adverse curated history

Benign lines of evidence:
- the benign hypothesis is verified
- every competing hypothesis was evaluated (with anomalies contradicted)
- no unexplained deviation
- consistently cleared curated history
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from asas.core.config import Config
from asas.domain.models import (
    Category,
    Classification,
    DeviationProfile,
    HypothesisClass,
    HypothesisStatus,
    InvestigationResult,
)
from asas.engine.deviation import residual
from asas.engine.hypotheses import CATALOG_BY_TYPE

UNRESOLVED = "UNRESOLVED"
# Every corroboration prefix (business meaning: config/business_context.toml).
CORROBORATION_CODES = (
    "HYPOTHESIS_VERIFIED",
    "PEER_DEVIATION",
    "CONFIRMED_AT_EVERY_PEER_LEVEL",
    "OWN_BOOK_HISTORY_DEVIATION",
    "RARE_COMBINATION",
    "RULE_DETECTION",
    "ADVERSE_CURATED_HISTORY",
    "COMPETING_ANOMALIES_CONTRADICTED",
    "NO_UNEXPLAINED_DEVIATION",
    "CLEARED_CURATED_HISTORY",
    "MISSING",
)


def _level(cfg: Config, name: str, bump: bool) -> str:
    levels = cfg.strings("classification", "levels")
    severities = cfg.section("classification", "severity")
    base = str(severities.get(name, severities[UNRESOLVED]))
    idx = levels.index(base) + (1 if bump else 0)
    return levels[min(idx, len(levels) - 1)]


def _confidence(lines: Sequence[str], cfg: Config) -> str:
    if len(lines) >= cfg.integer("classification", "confidence_high"):
        return "HIGH"
    if len(lines) >= cfg.integer("classification", "confidence_medium"):
        return "MEDIUM"
    return "LOW"


def _anomaly_lines(
    investigation: InvestigationResult,
    profile: DeviationProfile | None,
    rules: Sequence[str],
    cfg: Config,
) -> list[str]:
    conclusion = investigation.conclusion or ""
    lines = [f"HYPOTHESIS_VERIFIED:{conclusion}"]
    typology_signals = cfg.section("classification", "typology_signals")
    relevant = (
        set(investigation.unexplained_deviations)
        if investigation.unexplained_deviations and conclusion not in typology_signals
        else set(typology_signals.get(conclusion, []))
    )
    if profile is not None:
        verified = [d for d in profile.deviations if d.verified and d.signal in relevant]
        lines += [f"PEER_DEVIATION:{d.signal}" for d in verified]
        if any(d.confirming_levels == len(d.peers) and len(d.peers) > 1 for d in verified):
            lines.append("CONFIRMED_AT_EVERY_PEER_LEVEL")
        if any(d.self_history is not None and d.self_history.deviant for d in verified):
            lines.append("OWN_BOOK_HISTORY_DEVIATION")
        if profile.joint is not None and profile.joint.verified:
            lines.append("RARE_COMBINATION")
    lines += [f"RULE_DETECTION:{r}" for r in rules]
    if any(
        h.type == "RECURRING_BENIGN_CONTEXT" and h.status is HypothesisStatus.CONTRADICTED
        for h in investigation.hypotheses
    ):
        lines.append("ADVERSE_CURATED_HISTORY")
    return lines


def _benign_lines(investigation: InvestigationResult) -> list[str]:
    lines = [f"HYPOTHESIS_VERIFIED:{investigation.conclusion}"]
    anomalous = [h for h in investigation.hypotheses if h.klass is HypothesisClass.ANOMALOUS]
    if anomalous and all(h.status is HypothesisStatus.CONTRADICTED for h in anomalous):
        lines.append("COMPETING_ANOMALIES_CONTRADICTED")
    if not investigation.unexplained_deviations:
        lines.append("NO_UNEXPLAINED_DEVIATION")
    if any(
        h.type == "RECURRING_BENIGN_CONTEXT" and h.status is HypothesisStatus.SUPPORTED
        for h in investigation.hypotheses
    ):
        lines.append("CLEARED_CURATED_HISTORY")
    return lines


def classify(
    investigation: InvestigationResult | None,
    profile: DeviationProfile | None,
    rules: Sequence[str],
    notional_usd: Decimal | None,
    cfg: Config,
) -> Classification:
    material = (
        cfg.boolean("classification", "materiality_bump")
        and notional_usd is not None
        and notional_usd >= cfg.decimal("scoring", "materiality_usd")
    )
    unexplained = investigation.unexplained_deviations if investigation else ()
    explained = investigation.explained_deviations if investigation else ()
    raw = profile.outlyingness if profile is not None else Decimal(0)
    band = profile.band if profile is not None else "NONE"
    res_total, res_band = Decimal(0), "NONE"
    if profile is not None:
        _, res_total, res_band = residual(profile, unexplained, cfg)

    if investigation is None or investigation.conclusion is None:
        category, typology, lines = Category.UNRESOLVED, UNRESOLVED, []
        if investigation is not None:
            lines = [f"MISSING:{m}" for m in investigation.missing_evidence]
    elif investigation.conclusion_class is HypothesisClass.ANOMALOUS:
        typology = investigation.conclusion
        category = (
            Category.UNEXPLAINED_DEVIATION
            if CATALOG_BY_TYPE[typology].residual
            else Category.TYPED_ANOMALY
        )
        lines = _anomaly_lines(investigation, profile, rules, cfg)
    else:
        category, typology = Category.VERIFIED_BENIGN, investigation.conclusion
        lines = _benign_lines(investigation)

    confidence = _confidence(lines, cfg) if category is not Category.UNRESOLVED else "LOW"
    return Classification(
        category=category,
        typology=typology,
        severity=_level(cfg, typology, material and category is not Category.VERIFIED_BENIGN),
        confidence=confidence,
        corroboration=tuple(lines),
        outlyingness=raw,
        residual_outlyingness=res_total,
        band=band,
        residual_band=res_band,
        unexplained=tuple(unexplained),
        explained=tuple(explained),
    )
