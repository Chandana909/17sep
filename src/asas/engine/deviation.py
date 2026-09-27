"""Verified deviation analysis: how outlying an episode is, and whether that is trustworthy.

This replaces an unsupervised outlier model (e.g. Isolation Forest + SHAP) with a transparent,
verified equivalent. For every behavioural signal in `anomaly.signals`:

1. **Screen** against each peer population (desk+product, desk, global): mid-rank percentile
   and robust z (MAD, falling back to mean absolute deviation). `screen_mode` decides whether
   both or either must pass.
2. **Verify.** A deviation counts only if
   - at least `min_confirming_levels` peer populations of size >= `signals.min_peer_n` agree
     (not an artefact of one small group),
   - it is **stable**: it still deviates against the reference window shifted back by
     `stability_shift_days` (not a baseline wobble), and
   - no data-quality flag listed in `anomaly.quality_exclusions` taints that signal.
3. **Self-history.** The same comparison against the episode's own book history is reported
   and adds weight, but never verifies on its own.
4. **Combination rarity.** The rarest pair/triple of individually common facts (event
   signature, flags, magnitude bands) in the reference window: the multivariate view that a
   forest would capture, expressed as a named combination with its support count.

Outlyingness is a sum of named components with config weights. `residual()` recomputes it over
only the deviations that no verified benign explanation accounts for: the number that ranks
genuinely unexplained behaviour. Every threshold is in `[anomaly]`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from asas.core.config import Config
from asas.domain.models import (
    DeviationProfile,
    Episode,
    EpisodeSignals,
    JointRarity,
    PeerStat,
    ScoreComponent,
    SignalDeviation,
)
from asas.engine.capabilities import DataCapabilities
from asas.engine.items import episode_items, informative_items
from asas.engine.signals import (
    Baseline,
    SignalSet,
    level_key,
    median_of,
    percentile_of,
    period_start,
    robust_scale,
)

_Q = Decimal("0.0001")
_Z = Decimal("0.01")
JOINT = "joint"
BANDS = ("NONE", "LOW", "MEDIUM", "HIGH")


# ---------------------------------------------------------------- population comparisons


def _tail(cfg: Config, signal: str) -> str:
    tails = cfg.section("anomaly", "tails")
    return str(tails.get(signal, "upper"))


def _screen(percentile: Decimal, z: Decimal, tail: str, cfg: Config) -> bool:
    p_min = cfg.decimal("anomaly", "screen_percentile")
    z_min = cfg.decimal("anomaly", "screen_robust_z")
    upper = (percentile >= p_min, z >= z_min)
    lower = (percentile <= Decimal(100) - p_min, -z >= z_min)
    candidates = []
    if tail in ("upper", "both"):
        candidates.append(upper)
    if tail in ("lower", "both"):
        candidates.append(lower)
    combine = all if cfg.string("anomaly", "screen_mode") == "all" else any
    return any(combine(c) for c in candidates)


def peer_stat(
    population: Sequence[Decimal],
    value: Decimal,
    label: str,
    tail: str,
    cfg: Config,
    centre: tuple[Decimal, Decimal] | None = None,
) -> PeerStat:
    if centre is None:
        median = median_of(population)
        centre = (median, robust_scale(population, median, cfg))
    median, scale = centre
    cap = cfg.decimal("anomaly", "z_cap")
    if scale:
        z = ((value - median) / scale).quantize(_Z)
    else:  # a constant population: any departure is maximal, no departure is zero
        z = cap if value > median else (-cap if value < median else Decimal(0))
    pct = percentile_of(population, value)
    return PeerStat(
        level=label,
        n=len(population),
        median=median.quantize(_Q),
        scale=scale.quantize(_Q),
        percentile=pct,
        robust_z=z,
        deviant=_screen(pct, z, tail, cfg),
    )


def _stats(
    baseline: Baseline | None,
    signals: EpisodeSignals,
    name: str,
    value: Decimal,
    levels: Iterable[str],
    tail: str,
    cfg: Config,
) -> list[PeerStat]:
    if baseline is None:
        return []
    out = []
    for level in levels:
        key = level_key(signals, level)
        population = baseline.population(level, key, name)
        if population:
            centre = baseline.centre(level, key, name, cfg)
            out.append(peer_stat(population, value, f"{level}:{key}", tail, cfg, centre))
    return out


def _strength(confirming: Sequence[PeerStat], cfg: Config) -> Decimal:
    if not confirming:
        return Decimal(0)
    cap = cfg.decimal("anomaly", "z_cap")
    weakest = min(abs(s.robust_z) for s in confirming)
    return min(weakest / cap, Decimal(1)).quantize(_Q)


def signal_deviation(
    name: str,
    signals: EpisodeSignals,
    episode: Episode,
    baseline: Baseline | None,
    shifted: Baseline | None,
    cfg: Config,
) -> SignalDeviation | None:
    """Compare one signal with its peers; None when the value is absent or never screens."""
    value = signals.numeric.get(name)
    if value is None:
        return None
    tail = _tail(cfg, name)
    min_n = cfg.integer("signals", "min_peer_n")
    peers = _stats(baseline, signals, name, value, cfg.strings("anomaly", "peer_levels"), tail, cfg)
    if not any(p.deviant for p in peers):
        return None
    self_level = cfg.string("anomaly", "self_level")
    own = _stats(baseline, signals, name, value, (self_level,), tail, cfg)
    self_history = own[0] if own and own[0].n >= cfg.integer("anomaly", "self_min_n") else None

    confirming = [p for p in peers if p.deviant and p.n >= min_n]
    reasons: list[str] = []
    needed = cfg.integer("anomaly", "min_confirming_levels")
    if len(confirming) < needed:
        reasons.append(f"CONFIRMED_AT_{len(confirming)}_OF_{needed}_LEVELS")

    later = _stats(shifted, signals, name, value, cfg.strings("anomaly", "peer_levels"), tail, cfg)
    testable = [p for p in later if p.n >= min_n]
    stable: bool | None = any(p.deviant for p in testable) if testable else None
    if stable is False:
        reasons.append("UNSTABLE_VS_SHIFTED_REFERENCE")
    elif stable is None and cfg.boolean("anomaly", "require_stability"):
        reasons.append("STABILITY_UNTESTABLE")

    exclusions = cfg.section("anomaly", "quality_exclusions").get(name, [])
    tainted = sorted(set(episode.quality_flags) & set(exclusions))
    reasons.extend(f"DATA_QUALITY:{f}" for f in tainted)

    verified = not reasons
    if verified:
        reasons.append(f"VERIFIED_AT_{len(confirming)}_LEVELS")
    return SignalDeviation(
        signal=name,
        value=value,
        tail=tail,
        peers=tuple(peers),
        self_history=self_history,
        stable=stable,
        confirming_levels=len(confirming),
        verified=verified,
        strength=_strength(confirming, cfg) if verified else Decimal(0),
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------- combination rarity


@dataclass(frozen=True)
class ItemIndex:
    """Support counts of single items and of item combinations in one reference window."""

    reference_n: int
    counts: Mapping[tuple[str, ...], int]


def build_item_index(reference: Iterable[EpisodeSignals], cfg: Config) -> ItemIndex:
    order = cfg.integer("anomaly", "joint_max_order")
    counts: Counter[tuple[str, ...]] = Counter()
    n = 0
    for s in reference:
        n += 1
        items = informative_items(episode_items(s, cfg), cfg)
        for size in range(1, order + 1):
            counts.update(combinations(items, size))
    return ItemIndex(n, counts)


def joint_rarity(
    signals: EpisodeSignals, index: ItemIndex, cfg: Config, in_reference: bool = False
) -> JointRarity | None:
    """Leave-one-out: when the episode is itself in the reference window its own occurrence
    is not counted, so support means "seen in other episodes"."""
    own = 1 if in_reference else 0
    items = informative_items(episode_items(signals, cfg), cfg)
    min_item = cfg.integer("anomaly", "joint_min_item_support")
    common = [i for i in items if index.counts.get((i,), 0) - own >= min_item]
    best: tuple[int, tuple[str, ...], int] | None = None
    for size in range(2, cfg.integer("anomaly", "joint_max_order") + 1):
        for combo in combinations(common, size):
            support = index.counts.get(combo, 0) - own
            weakest = min(index.counts[(i,)] - own for i in combo)
            if best is None or (support, -weakest) < (best[0], -best[2]):
                best = (support, combo, weakest)
    if best is None:
        return None
    support, combo, weakest = best
    verified = index.reference_n - own >= cfg.integer(
        "anomaly", "joint_min_reference"
    ) and support <= cfg.integer("anomaly", "joint_max_support")
    return JointRarity(
        items=combo,
        support=support,
        reference_n=index.reference_n - own,
        min_item_support=weakest,
        verified=verified,
    )


# ---------------------------------------------------------------- outlyingness


def _band(total: Decimal, cfg: Config) -> str:
    if total >= cfg.decimal("anomaly", "band_high"):
        return "HIGH"
    if total >= cfg.decimal("anomaly", "band_medium"):
        return "MEDIUM"
    return "LOW" if total > 0 else "NONE"


def band_rank(band: str) -> int:
    return BANDS.index(band)


def outlyingness(
    deviations: Sequence[SignalDeviation],
    joint: JointRarity | None,
    keep: Iterable[str],
    cfg: Config,
) -> tuple[tuple[ScoreComponent, ...], Decimal, str]:
    """Named components over the verified deviations in `keep`."""
    kept = set(keep)
    chosen = [d for d in deviations if d.verified and d.signal in kept]
    weights = {k: Decimal(str(v)) for k, v in cfg.section("anomaly", "weights").items()}
    raw = {
        "peer_deviation": sum((d.strength for d in chosen), Decimal()),
        "breadth": Decimal(max(len(chosen) - 1, 0)),
        "self_history": Decimal(
            sum(1 for d in chosen if d.self_history is not None and d.self_history.deviant)
        ),
        "joint_rarity": Decimal(1)
        if joint is not None and joint.verified and JOINT in kept
        else Decimal(0),
    }
    detail = {
        "peer_deviation": ", ".join(f"{d.signal} ({d.strength})" for d in chosen) or "none",
        "breadth": f"{len(chosen)} verified deviating signals",
        "self_history": "deviates from own book history"
        if raw["self_history"]
        else "no book-history deviation",
        "joint_rarity": " + ".join(joint.items) if joint and raw["joint_rarity"] else "none",
    }
    components = tuple(
        ScoreComponent(
            name=name,
            raw=value.quantize(_Q),
            weight=weights[name],
            contribution=(value * weights[name]).quantize(_Q),
            detail=detail[name],
        )
        for name, value in raw.items()
    )
    total = sum((c.contribution for c in components), Decimal()).quantize(_Q)
    return components, total, _band(total, cfg)


def deviation_profile(
    episode: Episode,
    signals: EpisodeSignals,
    signal_set: SignalSet,
    index: ItemIndex | None,
    caps: DataCapabilities,
    cfg: Config,
    in_reference: bool = False,
) -> DeviationProfile:
    baseline = signal_set.baseline_for(signals.eval_time)
    shifted = signal_set.shifted_for(signals.eval_time)
    notes: list[str] = []
    screened: list[str] = []
    deviations: list[SignalDeviation] = []
    available = baseline is not None and baseline.total > 0
    if not available:
        notes.append("NO_BASELINE")
    for name in cfg.strings("anomaly", "signals"):
        missing = caps.missing_for_signal(name)
        if missing:
            notes.append(f"FIELD_UNAVAILABLE:{name}:{'+'.join(sorted(missing))}")
            continue
        screened.append(name)
        if not available:
            continue
        d = signal_deviation(name, signals, episode, baseline, shifted, cfg)
        if d is not None:
            deviations.append(d)
    joint = (
        joint_rarity(signals, index, cfg, in_reference) if index is not None and available else None
    )
    verified = [d.signal for d in deviations if d.verified]
    if joint is not None and joint.verified:
        verified.append(JOINT)
    components, total, band = outlyingness(deviations, joint, verified, cfg)
    return DeviationProfile(
        episode_id=episode.episode_id,
        available=available,
        baseline_period=period_start(signals.eval_time) if available else None,
        screened=tuple(screened),
        deviations=tuple(deviations),
        joint=joint,
        verified=tuple(verified),
        components=components,
        outlyingness=total,
        band=band,
        notes=tuple(notes),
    )


def residual(
    profile: DeviationProfile, unexplained: Iterable[str], cfg: Config
) -> tuple[tuple[ScoreComponent, ...], Decimal, str]:
    """Outlyingness of only the deviations no verified benign explanation accounts for."""
    keep = set(unexplained) & set(profile.verified)
    return outlyingness(profile.deviations, profile.joint, keep, cfg)


def build_profiles(
    episodes: Sequence[Episode],
    signal_set: SignalSet,
    caps: DataCapabilities,
    cfg: Config,
) -> dict[str, DeviationProfile]:
    indexes: dict[object, tuple[ItemIndex, frozenset[str]]] = {}
    out: dict[str, DeviationProfile] = {}
    for ep in episodes:
        s = signal_set.signals[ep.episode_id]
        period = period_start(s.eval_time)
        if period not in indexes:
            ids = signal_set.reference_for(s.eval_time)
            index = build_item_index((signal_set.signals[i] for i in ids), cfg)
            indexes[period] = (index, frozenset(ids))
        index, members = indexes[period]
        out[ep.episode_id] = deviation_profile(
            ep, s, signal_set, index, caps, cfg, ep.episode_id in members
        )
    return out
