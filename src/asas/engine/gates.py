"""Data gates: is the input fit for a bulk-review run at all?

Surveillance on incomplete data is a control failure in its own right. Before any case is
decided, the pipeline checks the point-in-time source for:

- **freshness:** the newest trade event / alert record is no older than
  `pipeline.max_staleness_hours` before `as_of` (a feed that stopped arriving)
- **volume:** records in the last `pipeline.volume_window_days` are at least
  `pipeline.min_volume_ratio` of the median of the previous `pipeline.volume_baseline_windows`
  windows (a partial or truncated load)

A failed gate never stops investigation. It marks the run degraded and blocks bulk
proposals (reason DATA_GATE:...), so every case is reviewed individually until the data is
fixed and the run repeated.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from asas.core.config import Config
from asas.data.ingest import SourceBundle


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str

    @property
    def reason(self) -> str:
        return f"DATA_GATE:{self.name}"


def _freshness(name: str, times: Sequence[datetime], as_of: datetime, cfg: Config) -> GateResult:
    limit = timedelta(hours=cfg.integer("pipeline", "max_staleness_hours"))
    if not times:
        return GateResult(f"NO_{name}", False, f"no {name.lower()} recorded at or before as_of")
    lag = as_of - max(times)
    hours = Decimal(str(lag.total_seconds())) / Decimal(3600)
    ok = lag <= limit
    return GateResult(
        f"STALE_{name}", ok, f"newest {name.lower()} is {hours:.1f}h old (limit {limit})"
    )


def _volume(name: str, times: Sequence[datetime], as_of: datetime, cfg: Config) -> GateResult:
    window = timedelta(days=cfg.integer("pipeline", "volume_window_days"))
    windows = cfg.integer("pipeline", "volume_baseline_windows")
    ratio_min = cfg.decimal("pipeline", "min_volume_ratio")

    def count(k: int) -> int:
        hi, lo = as_of - k * window, as_of - (k + 1) * window
        return sum(1 for t in times if lo < t <= hi)

    current = count(0)
    baseline = [count(k) for k in range(1, windows + 1)]
    median = Decimal(str(statistics.median(baseline))) if baseline else Decimal(0)
    if not median:
        return GateResult(f"LOW_VOLUME_{name}", True, "no history to compare volume against")
    ratio = Decimal(current) / median
    return GateResult(
        f"LOW_VOLUME_{name}",
        ratio >= ratio_min,
        f"{current} in the last window vs median {median} (ratio {ratio:.2f}, min {ratio_min})",
    )


def data_gates(source: SourceBundle, as_of: datetime, cfg: Config) -> list[GateResult]:
    trade_times = [e.record_time for e in source.trade_events]
    alert_times = [a.record_time for a in source.alerts]
    return [
        _freshness("TRADE_EVENTS", trade_times, as_of, cfg),
        _freshness("ALERTS", alert_times, as_of, cfg),
        _volume("TRADE_EVENTS", trade_times, as_of, cfg),
        _volume("ALERTS", alert_times, as_of, cfg),
    ]
