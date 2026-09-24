"""Point-in-time episode signals and frozen peer baselines.

Signals replace the ML feature layer: every value is computed from contract fields recorded
at or before the evaluation time, and every comparison names its peer population, size and
reference window. Baselines are frozen per calendar month (reference = lookback window that
ends at the month start), so the same episode always scores the same way.
"""

from __future__ import annotations

import bisect
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from asas.core.config import Config
from asas.domain.models import (
    Alert,
    Episode,
    EpisodeSignals,
    EventType,
    LinkKind,
    OutlierEvidence,
    TradeEvent,
)

NUMERIC_SIGNALS: Mapping[str, str] = {
    "n_trades": "trades in the episode",
    "n_events": "lifecycle events in the episode",
    "n_amends": "amendment events",
    "n_cancels": "cancellation events",
    "n_alerts": "SCP alerts on the episode",
    "n_rules_fired": "distinct SCP rules that alerted",
    "span_hours": "hours from first to last event",
    "max_price_change_pct": "largest price change between consecutive versions, percent",
    "max_qty_change_pct": "largest quantity change between consecutive versions, percent",
    "notional_usd_max": "largest notional in USD",
    "booking_latency_hours_max": "largest record_time minus event_time, hours",
    "rebook_count": "cancel/rebook links inside the episode",
    "rebook_price_change_pct": "largest price gap between a cancelled trade and its rebook, pct",
    "hours_to_period_end_min": "fewest hours between an event and its month end",
    "hours_first_amend": "hours from booking to the first amendment",
    "book_signature_recurrence": "prior episodes in the same book with the same event signature",
    "instrument_signature_recurrence": "prior same-signature episodes on the same instrument",
    "signature_rarity_pct": "100 minus the share of the signature in the reference window, percent",
}
FLAG_SIGNALS: Mapping[str, str] = {
    "near_period_end": "an event falls within the period-end cutoff",
    "crosses_period_end": "one trade has versions in two different months",
    "price_round_trip": "a price was moved and later restored",
    "has_amend": "the episode contains an amendment",
    "has_cancel": "the episode contains a cancellation",
    "has_rebook_link": "the episode contains a cancel/rebook link",
    "lifecycle_valid": "every trade lifecycle is structurally valid",
}
LABEL_SIGNALS: Mapping[str, str] = {
    "sequence_signature": "ordered event types across the episode",
    "desk": "desk",
    "book": "book",
    "product_type": "product type",
    "instrument_id": "instrument",
    "desk_product": "desk and product type",
}
LIFECYCLE_ISSUES = frozenset(
    {"NO_LIFECYCLE", "FIRST_EVENT_NOT_NEW", "VERSION_GAP", "EVENTS_AFTER_CANCEL"}
)
_PCT = Decimal(100)
_HOUR = Decimal(3600)
_Q = Decimal("0.0001")


def signal_kind(name: str) -> str | None:
    if name in NUMERIC_SIGNALS:
        return "numeric"
    if name in FLAG_SIGNALS:
        return "flag"
    if name in LABEL_SIGNALS:
        return "label"
    return None


def period_end(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    year, month = (ts.year + 1, 1) if ts.month == 12 else (ts.year, ts.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def period_start(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return datetime(ts.year, ts.month, 1, tzinfo=UTC)


def _hours(delta: timedelta) -> Decimal:
    return (Decimal(str(delta.total_seconds())) / _HOUR).quantize(_Q)


def _pct_change(old: Decimal, new: Decimal) -> Decimal:
    return (abs(new - old) / abs(old) * _PCT).quantize(_Q)


def base_signals(
    episode: Episode,
    events: Sequence[TradeEvent],
    alerts: Sequence[Alert],
    eval_time: datetime,
    cfg: Config,
) -> EpisodeSignals:
    """Signals computable from the episode alone (history-dependent ones are added later)."""
    evs = sorted(
        (e for e in events if e.record_time <= eval_time),
        key=lambda e: (e.event_time, e.trade_id, e.version),
    )
    als = [a for a in alerts if a.record_time <= eval_time]
    by_trade: dict[str, list[TradeEvent]] = defaultdict(list)
    for e in evs:
        by_trade[e.trade_id].append(e)
    for trade in by_trade.values():
        trade.sort(key=lambda e: e.version)

    numeric: dict[str, Decimal] = {
        "n_trades": Decimal(len(by_trade)),
        "n_events": Decimal(len(evs)),
        "n_amends": Decimal(sum(e.event_type is EventType.AMEND for e in evs)),
        "n_cancels": Decimal(sum(e.event_type is EventType.CANCEL for e in evs)),
        "n_alerts": Decimal(len(als)),
        "n_rules_fired": Decimal(len({a.rule_id for a in als})),
    }
    flags: dict[str, bool] = {
        "has_amend": numeric["n_amends"] > 0,
        "has_cancel": numeric["n_cancels"] > 0,
    }
    if evs:
        numeric["span_hours"] = _hours(evs[-1].event_time - evs[0].event_time)
        numeric["booking_latency_hours_max"] = max(
            _hours(e.record_time - e.event_time) for e in evs
        )
        numeric["hours_to_period_end_min"] = min(
            _hours(period_end(e.event_time) - e.event_time) for e in evs
        )
        flags["near_period_end"] = numeric["hours_to_period_end_min"] <= cfg.decimal(
            "signals", "period_end_cutoff_hours"
        )
        notionals = [e.notional_usd for e in evs if e.notional_usd is not None]
        if notionals:
            numeric["notional_usd_max"] = max(notionals)

    price_changes: list[Decimal] = []
    qty_changes: list[Decimal] = []
    first_amend: list[Decimal] = []
    round_trip = False
    crosses = False
    min_move = cfg.decimal("signals", "round_trip_min_pct")
    tolerance = cfg.decimal("signals", "round_trip_tolerance_pct")
    for versions in by_trade.values():
        if len({period_start(e.event_time) for e in versions}) > 1:
            crosses = True
        for prev, cur in pairwise(versions):
            if prev.price and cur.price is not None and cur.event_type is not EventType.CANCEL:
                price_changes.append(_pct_change(prev.price, cur.price))
            if (
                prev.quantity
                and cur.quantity is not None
                and cur.event_type is not EventType.CANCEL
            ):
                qty_changes.append(_pct_change(prev.quantity, cur.quantity))
        amends = [e for e in versions if e.event_type is EventType.AMEND]
        if amends and versions[0].event_type is EventType.NEW:
            first_amend.append(_hours(amends[0].event_time - versions[0].event_time))
        base = versions[0].price
        moved = False
        for e in versions[1:]:
            if not base or e.price is None or e.event_type is EventType.CANCEL:
                continue
            change = _pct_change(base, e.price)
            if change >= min_move:
                moved = True
            elif moved and change <= tolerance:
                round_trip = True
    if price_changes:
        numeric["max_price_change_pct"] = max(price_changes)
    if qty_changes:
        numeric["max_qty_change_pct"] = max(qty_changes)
    if first_amend:
        numeric["hours_first_amend"] = min(first_amend)
    flags["price_round_trip"] = round_trip
    flags["crosses_period_end"] = crosses

    rebooks = [
        x
        for x in episode.links
        if x.kind in (LinkKind.REBOOK_OF, LinkKind.AGENT_LINK)
        and x.src in by_trade
        and x.dst in by_trade
    ]
    numeric["rebook_count"] = Decimal(len(rebooks))
    flags["has_rebook_link"] = bool(rebooks)
    rebook_moves: list[Decimal] = []
    for link in rebooks:
        new_price = by_trade[link.src][0].price
        live = [e for e in by_trade[link.dst] if e.event_type is not EventType.CANCEL]
        old_price = live[-1].price if live else None
        if new_price is not None and old_price:
            rebook_moves.append(_pct_change(old_price, new_price))
    if rebook_moves:
        numeric["rebook_price_change_pct"] = max(rebook_moves)
    flags["lifecycle_valid"] = not (set(episode.quality_flags) & LIFECYCLE_ISSUES)

    rebook_sources = {x.src for x in rebooks}
    tokens: list[str] = []
    for e in evs:
        token = e.event_type.value
        if e.event_type is EventType.NEW and e.trade_id in rebook_sources:
            token = "REBOOK"
        tokens.append(token)
    max_tokens = cfg.integer("signals", "signature_max_tokens")
    signature = ">".join(tokens[:max_tokens]) or "ALERT_ONLY"
    labels = {
        "sequence_signature": signature,
        "desk": episode.desk,
        "book": episode.books[0] if episode.books else "UNKNOWN",
        "product_type": episode.product_types[0] if episode.product_types else "UNKNOWN",
        "instrument_id": episode.instrument_ids[0] if episode.instrument_ids else "UNKNOWN",
    }
    labels["desk_product"] = f"{labels['desk']}|{labels['product_type']}"
    return EpisodeSignals(
        episode_id=episode.episode_id,
        eval_time=eval_time,
        numeric=numeric,
        flags=flags,
        labels=labels,
    )


# ---------------------------------------------------------------- baselines


def _percentile(sorted_values: Sequence[Decimal], x: Decimal) -> Decimal:
    """Mid-rank percentile: ties share the middle of their rank range, so a value equal to the
    common case never looks extreme."""
    below = bisect.bisect_left(sorted_values, x)
    equal = bisect.bisect_right(sorted_values, x) - below
    rank = Decimal(below) + Decimal(equal) / Decimal(2)
    return (rank / Decimal(len(sorted_values)) * _PCT).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Baseline:
    period_start: datetime
    reference_start: datetime
    values: Mapping[tuple[str, str], Mapping[str, tuple[Decimal, ...]]]
    signature_share: Mapping[str, Decimal]
    total: int

    def compare(self, signals: EpisodeSignals, name: str, cfg: Config) -> OutlierEvidence | None:
        value = signals.numeric.get(name)
        if value is None:
            return None
        min_n = cfg.integer("signals", "min_peer_n")
        for level in cfg.strings("signals", "peer_levels"):
            key = "*" if level == "global" else signals.labels.get(level, "")
            population = self.values.get((level, key), {}).get(name, ())
            if len(population) < min_n:
                continue
            median = Decimal(str(statistics.median(population)))
            mad = Decimal(str(statistics.median([abs(v - median) for v in population])))
            scale = cfg.decimal("signals", "mad_scale") * mad
            z = ((value - median) / scale).quantize(Decimal("0.01")) if scale else None
            return OutlierEvidence(
                signal=name,
                value=value,
                percentile=_percentile(population, value),
                robust_z=z,
                peer_level=f"{level}:{key}",
                peer_n=len(population),
            )
        return None


def build_baseline(
    start_of_period: datetime, reference: Iterable[EpisodeSignals], cfg: Config
) -> Baseline:
    lookback = timedelta(days=cfg.integer("signals", "baseline_lookback_days"))
    grouped: dict[tuple[str, str], dict[str, list[Decimal]]] = defaultdict(
        lambda: defaultdict(list)
    )
    signatures: Counter[str] = Counter()
    total = 0
    levels = cfg.strings("signals", "peer_levels")
    for s in reference:
        total += 1
        signatures[s.labels["sequence_signature"]] += 1
        for level in levels:
            key = "*" if level == "global" else s.labels.get(level, "")
            for name, value in s.numeric.items():
                grouped[(level, key)][name].append(value)
    frozen = {k: {n: tuple(sorted(v)) for n, v in d.items()} for k, d in grouped.items()}
    share = {sig: (Decimal(c) / Decimal(total)) for sig, c in signatures.items()} if total else {}
    return Baseline(start_of_period, start_of_period - lookback, frozen, share, total)


# ---------------------------------------------------------------- engine


@dataclass(frozen=True)
class SignalSet:
    signals: Mapping[str, EpisodeSignals]
    baselines: Mapping[datetime, Baseline]

    def baseline_for(self, eval_time: datetime) -> Baseline | None:
        return self.baselines.get(period_start(eval_time))


def compute_signals(
    episodes: Sequence[Episode],
    events_by_trade: Mapping[str, Sequence[TradeEvent]],
    alerts_by_trade: Mapping[str, Sequence[Alert]],
    eval_times: Mapping[str, datetime],
    cfg: Config,
) -> SignalSet:
    base: dict[str, EpisodeSignals] = {}
    ends: dict[str, datetime] = {}
    for ep in episodes:
        evs = [e for t in ep.trade_ids for e in events_by_trade.get(t, ())]
        als = [a for t in ep.trade_ids for a in alerts_by_trade.get(t, ())]
        base[ep.episode_id] = base_signals(ep, evs, als, eval_times[ep.episode_id], cfg)
        ends[ep.episode_id] = ep.end

    lookback = timedelta(days=cfg.integer("signals", "baseline_lookback_days"))
    periods = sorted({period_start(t) for t in eval_times.values()})
    by_end = sorted(ends.items(), key=lambda kv: kv[1])
    end_times = [t for _, t in by_end]
    baselines: dict[datetime, Baseline] = {}
    for p in periods:
        lo = bisect.bisect_left(end_times, p - lookback)
        hi = bisect.bisect_left(end_times, p)
        baselines[p] = build_baseline(p, (base[eid] for eid, _ in by_end[lo:hi]), cfg)

    window = timedelta(days=cfg.integer("signals", "recurrence_window_days"))
    index: dict[tuple[str, str, str], list[datetime]] = defaultdict(list)
    for eid, end in by_end:
        s = base[eid]
        sig = s.labels["sequence_signature"]
        index[("book", s.labels["book"], sig)].append(end)
        index[("instrument", s.labels["instrument_id"], sig)].append(end)

    final: dict[str, EpisodeSignals] = {}
    for ep in episodes:
        s = base[ep.episode_id]
        sig = s.labels["sequence_signature"]
        numeric = dict(s.numeric)
        for scope, label in (("book", "book"), ("instrument", "instrument_id")):
            times = index[(scope, s.labels[label], sig)]
            lo = bisect.bisect_left(times, ep.start - window)
            hi = bisect.bisect_left(times, ep.start)
            numeric[f"{scope}_signature_recurrence"] = Decimal(max(hi - lo, 0))
        baseline = baselines.get(period_start(s.eval_time))
        if baseline is not None and baseline.total:
            share = baseline.signature_share.get(sig, Decimal(0))
            numeric["signature_rarity_pct"] = ((Decimal(1) - share) * _PCT).quantize(
                Decimal("0.01")
            )
        final[ep.episode_id] = s.model_copy(update={"numeric": numeric})
    return SignalSet(final, baselines)
