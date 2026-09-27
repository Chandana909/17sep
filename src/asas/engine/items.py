"""Discretised episode facts ("items"): event signature, lifecycle flags, desk, magnitude bands.

Shared by pattern discovery (frequent itemsets) and deviation analysis (rare combinations of
individually common facts). Bands come from `discovery.bands`, so the business moves a band
edge in config, never in code.
"""

from __future__ import annotations

from decimal import Decimal

from asas.core.config import Config
from asas.domain.models import EpisodeSignals
from asas.engine.signals import FLAG_SIGNALS


def episode_items(signals: EpisodeSignals, cfg: Config) -> frozenset[str]:
    items = {f"sig={signals.labels['sequence_signature']}", f"desk={signals.labels['desk']}"}
    items |= {f"flag:{name}" for name in FLAG_SIGNALS if signals.flags.get(name)}
    for name, cuts in cfg.section("discovery", "bands").items():
        value = signals.numeric.get(name)
        if value is None:
            continue
        edges = [Decimal(c) for c in cuts]
        band = sum(1 for c in edges if value >= c)
        items.add(f"band:{name}:{band}")
    return frozenset(items)


def informative_items(items: frozenset[str], cfg: Config) -> tuple[str, ...]:
    """Items that describe behaviour (not organisation), for combination rarity."""
    prefixes = cfg.strings("anomaly", "joint_ignored_prefixes")
    ignored = set(cfg.strings("anomaly", "joint_ignored_items"))
    return tuple(
        sorted(i for i in items if i not in ignored and not any(i.startswith(p) for p in prefixes))
    )
