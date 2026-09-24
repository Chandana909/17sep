"""Deterministic attention scoring (replaces the ML ensemble score + SHAP).

The score is a sum of named components with config weights. Each component carries its raw
value and a human-readable detail, so the breakdown *is* the explanation. Outlier evidence
names the peer population, its size and the percentile. Scores only order work and raise
attention; they never make a case eligible for bulk review.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from asas.core.config import Config
from asas.domain.models import (
    Episode,
    EpisodeScore,
    EpisodeSignals,
    OutlierEvidence,
    ScoreComponent,
)
from asas.engine.signals import Baseline

_Q = Decimal("0.0001")
_PCT = Decimal(100)
BUCKETS = ("B0", "B1", "B2", "B3")
BUCKET_ORDER = {b: i for i, b in enumerate(BUCKETS)}


def _component(name: str, raw: Decimal, weight: Decimal, detail: str) -> ScoreComponent:
    return ScoreComponent(
        name=name,
        raw=raw.quantize(_Q),
        weight=weight,
        contribution=(raw * weight).quantize(_Q),
        detail=detail,
    )


def score_episode(
    episode: Episode,
    signals: EpisodeSignals,
    baseline: Baseline | None,
    alert_rule_ids: Iterable[str],
    rfi_open: bool,
    cfg: Config,
) -> EpisodeScore:
    weights = cfg.section("scoring", "weights")
    w = {k: Decimal(str(v)) for k, v in weights.items()}
    components: list[ScoreComponent] = []
    degraded: list[str] = []

    severities = cfg.section("scoring", "rule_severity")
    severity_weights = cfg.section("scoring", "rule_severity_weights")
    default_severity = cfg.string("scoring", "unknown_rule_severity")
    rules = sorted(set(alert_rule_ids))
    rule_raw = sum(
        (Decimal(str(severity_weights[severities.get(r, default_severity)])) for r in rules),
        Decimal(),
    )
    components.append(
        _component(
            "detections", rule_raw, w["detections"], f"rules alerted: {', '.join(rules) or 'none'}"
        )
    )

    outliers: list[OutlierEvidence] = []
    high = cfg.decimal("scoring", "outlier_percentile")
    if baseline is None or baseline.total == 0:
        degraded.append("NO_BASELINE")
    else:
        for name in cfg.strings("scoring", "outlier_signals"):
            evidence = baseline.compare(signals, name, cfg)
            if evidence is None:
                continue
            if evidence.peer_level.startswith("global"):
                degraded.append(f"PEER_FALLBACK_GLOBAL:{name}")
            if evidence.percentile >= high:
                outliers.append(evidence)
    outlier_raw = sum(((o.percentile - high) / (_PCT - high) for o in outliers), Decimal())
    components.append(
        _component(
            "outliers",
            outlier_raw,
            w["outliers"],
            "; ".join(
                f"{o.signal} p{o.percentile} vs {o.peer_level} (n={o.peer_n})" for o in outliers
            )
            or "no signal above the outlier percentile",
        )
    )

    rarity = signals.numeric.get("signature_rarity_pct")
    rarity_min = cfg.decimal("scoring", "rarity_min_pct")
    rarity_raw = rarity / _PCT if rarity is not None and rarity >= rarity_min else Decimal()
    components.append(
        _component(
            "rarity",
            rarity_raw,
            w["rarity"],
            f"signature rarity {rarity if rarity is not None else 'n/a'} percent",
        )
    )

    notional = signals.numeric.get("notional_usd_max", Decimal())
    materiality = cfg.decimal("scoring", "materiality_usd")
    components.append(
        _component(
            "materiality",
            Decimal(1) if notional >= materiality else Decimal(),
            w["materiality"],
            f"max notional {notional} USD",
        )
    )

    blocking = set(cfg.strings("scoring", "quality_flags_scored"))
    quality = sorted(set(episode.quality_flags) & blocking)
    components.append(
        _component(
            "data_quality",
            Decimal(len(quality)),
            w["data_quality"],
            ", ".join(quality) or "no quality flags",
        )
    )

    total = sum((c.contribution for c in components), Decimal()).quantize(_Q)
    band = "NORMAL"
    if total >= cfg.decimal("scoring", "band_high"):
        band = "HIGH"
    elif total >= cfg.decimal("scoring", "band_elevated"):
        band = "ELEVATED"

    overrides: list[str] = []
    if rfi_open:
        overrides.append("OPEN_RFI")
    if notional >= cfg.decimal("scoring", "override_materiality_usd"):
        overrides.append("ABOVE_MATERIALITY")
    if set(episode.books) & set(cfg.strings("scoring", "watchlist_books")):
        overrides.append("WATCHLIST_BOOK")
    bucket = "B0" if overrides else {"HIGH": "B1", "ELEVATED": "B2"}.get(band, "B3")
    return EpisodeScore(
        episode_id=episode.episode_id,
        total=total,
        band=band,
        bucket=bucket,
        components=tuple(components),
        outliers=tuple(outliers),
        overrides=tuple(overrides),
        degraded=tuple(sorted(set(degraded))),
    )


def queue_key(score: EpisodeScore, episode: Episode) -> tuple[int, Decimal, str, str]:
    """Strict total order: bucket, score desc, oldest first, id (deterministic pagination)."""
    return (BUCKET_ORDER[score.bucket], -score.total, episode.start.isoformat(), episode.episode_id)
