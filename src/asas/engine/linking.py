"""Canonical episode construction.

Two-stage assembly:
  1. strong core  - same TRADE_ID lifecycle + ORIGINAL_TRADE_ID lineage (tier S)
  2. constrained medium attachments - shared URN_REF / ALTERNATE_TRADE_ID (tier M), accepted
     only if the key is not degenerate, the key's fanout is bounded, the trades are close in
     time and trade the same instrument, and the merged episode stays within its max span.
Human-confirmed agent links (tier A) join as an overlay under the same guards.
Unproven but plausible relationships (untagged cancel/rebook) are emitted as residue pairs
for the Investigation Agent; they never change canonical episodes on their own.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from asas.core.config import Config
from asas.core.ids import content_hash, stable_id
from asas.domain.models import (
    Alert,
    Episode,
    EventType,
    LinkKind,
    LinkStatus,
    LinkTier,
    TradeEvent,
    TradeLink,
    UnresolvedPair,
)


@dataclass(frozen=True)
class LinkingResult:
    episodes: tuple[Episode, ...]
    unresolved: tuple[UnresolvedPair, ...]
    episode_of_trade: Mapping[str, str]
    episode_of_alert: Mapping[str, str]
    version: str


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        root, child = (ra, rb) if ra < rb else (rb, ra)
        self.parent[child] = root
        return root


def linking_version(cfg: Config) -> str:
    return f"{cfg.string('linking', 'version')}:{content_hash(cfg.section('linking'))[:8]}"


class _Guards:
    def __init__(self, cfg: Config) -> None:
        self.max_span = timedelta(hours=cfg.integer("linking", "max_span_hours"))
        self.medium_gap = timedelta(hours=cfg.integer("linking", "medium_max_gap_hours"))
        self.max_fanout = cfg.integer("linking", "max_key_fanout")
        self.degenerate = {v.upper() for v in cfg.strings("linking", "degenerate_values")}
        self.patterns = [re.compile(p) for p in cfg.strings("linking", "degenerate_patterns")]

    def is_degenerate(self, value: str | None) -> bool:
        if value is None:
            return True
        text = value.strip().upper()
        return text in self.degenerate or any(p.fullmatch(text) for p in self.patterns)


def _lifecycle_issues(events: Sequence[TradeEvent]) -> list[str]:
    if not events:
        return ["NO_LIFECYCLE"]
    issues: list[str] = []
    if events[0].event_type is not EventType.NEW:
        issues.append("FIRST_EVENT_NOT_NEW")
    versions = [e.version for e in events]
    if versions != list(range(versions[0], versions[0] + len(versions))):
        issues.append("VERSION_GAP")
    types = [e.event_type for e in events]
    if EventType.CANCEL in types and types.index(EventType.CANCEL) != len(types) - 1:
        issues.append("EVENTS_AFTER_CANCEL")
    return issues


def build_episodes(
    events: Iterable[TradeEvent],
    alerts: Iterable[Alert],
    cfg: Config,
    overlay: Sequence[TradeLink] = (),
) -> LinkingResult:
    guards = _Guards(cfg)
    version = linking_version(cfg)
    by_trade: dict[str, list[TradeEvent]] = defaultdict(list)
    for e in events:
        by_trade[e.trade_id].append(e)
    for trade in by_trade.values():
        trade.sort(key=lambda e: (e.version, e.event_time))
    alert_list = sorted(alerts, key=lambda a: a.alert_id)
    trade_ids = sorted(set(by_trade) | {a.trade_id for a in alert_list})

    first_time: dict[str, datetime] = {}
    last_time: dict[str, datetime] = {}
    for t in trade_ids:
        times = [e.event_time for e in by_trade.get(t, [])] or [
            a.alert_time for a in alert_list if a.trade_id == t
        ]
        first_time[t], last_time[t] = min(times), max(times)
    instrument = {t: by_trade[t][0].instrument_id for t in by_trade}

    uf = _UnionFind(trade_ids)
    span_lo = dict(first_time)
    span_hi = dict(last_time)
    instruments: dict[str, set[str]] = {
        t: {instrument[t]} if t in instrument else set() for t in trade_ids
    }
    accepted: list[TradeLink] = []
    rejected: list[TradeLink] = []
    flags: dict[str, set[str]] = defaultdict(set)

    def attempt(link: TradeLink) -> None:
        ra, rb = uf.find(link.src), uf.find(link.dst)
        if ra == rb:
            accepted.append(link)
            return
        lo, hi = min(span_lo[ra], span_lo[rb]), max(span_hi[ra], span_hi[rb])
        reason = ""
        if hi - lo > guards.max_span:
            reason = "OVER_SPAN"
        elif link.tier is not LinkTier.STRONG and instruments[ra] != instruments[rb]:
            reason = "CROSS_INSTRUMENT"
        if reason:
            rejected.append(
                link.model_copy(update={"status": LinkStatus.REJECTED, "reason": reason})
            )
            flags[link.src].add(f"LINK_REJECTED_{reason}")
            flags[link.dst].add(f"LINK_REJECTED_{reason}")
            return
        root = uf.union(ra, rb)
        span_lo[root], span_hi[root] = lo, hi
        instruments[root] = instruments[ra] | instruments[rb]
        accepted.append(link)

    # stage 1: strong lineage
    for t in sorted(by_trade):
        original = next((e.original_trade_id for e in by_trade[t] if e.original_trade_id), None)
        if original is None or original == t:
            continue
        if guards.is_degenerate(original):
            flags[t].add("DEGENERATE_ORIGINAL_TRADE_ID")
            continue
        if original not in by_trade:
            flags[t].add("DANGLING_ORIGINAL_TRADE_ID")
            continue
        attempt(
            TradeLink(
                src=t,
                dst=original,
                kind=LinkKind.REBOOK_OF,
                tier=LinkTier.STRONG,
                status=LinkStatus.CANONICAL,
                provenance=f"deterministic:{version}",
                reason="ORIGINAL_TRADE_ID",
                evidence={"ORIGINAL_TRADE_ID": original},
            )
        )

    # human-confirmed agent links
    for link in sorted(overlay, key=lambda x: (x.src, x.dst)):
        if link.src in uf.parent and link.dst in uf.parent and link.status is LinkStatus.CANONICAL:
            attempt(link)

    # stage 2: constrained medium keys
    for key_name, kind in (
        ("URN_REF", LinkKind.SHARED_URN),
        ("ALTERNATE_TRADE_ID", LinkKind.ALTERNATE_ID),
    ):
        index: dict[str, set[str]] = defaultdict(set)
        for t in sorted(by_trade):
            value = (
                by_trade[t][0].urn_ref
                if key_name == "URN_REF"
                else by_trade[t][0].alternate_trade_id
            )
            if value is None:
                continue
            if guards.is_degenerate(value):
                flags[t].add(f"DEGENERATE_{key_name}")
                continue
            index[value.strip()].add(t)
            if key_name == "ALTERNATE_TRADE_ID" and value.strip() in by_trade:
                index[value.strip()].add(value.strip())
        for value in sorted(index):
            members = sorted(index[value])
            if len(members) > guards.max_fanout:
                for t in members:
                    flags[t].add(f"HUB_DEMOTED_{key_name}")
                continue
            anchor = members[0]
            for other in members[1:]:
                link = TradeLink(
                    src=other,
                    dst=anchor,
                    kind=kind,
                    tier=LinkTier.MEDIUM,
                    status=LinkStatus.CANONICAL,
                    provenance=f"deterministic:{version}",
                    reason=key_name,
                    evidence={key_name: value},
                )
                if abs(first_time[other] - first_time[anchor]) > guards.medium_gap:
                    rejected.append(
                        link.model_copy(
                            update={"status": LinkStatus.REJECTED, "reason": "OVER_GAP"}
                        )
                    )
                    continue
                attempt(link)

    components: dict[str, list[str]] = defaultdict(list)
    for t in trade_ids:
        components[uf.find(t)].append(t)
    alerts_by_trade: dict[str, list[Alert]] = defaultdict(list)
    for a in alert_list:
        alerts_by_trade[a.trade_id].append(a)

    episodes: list[Episode] = []
    episode_of_trade: dict[str, str] = {}
    episode_of_alert: dict[str, str] = {}
    for members in components.values():
        members.sort()
        member_set = set(members)
        ep_events = [e for t in members for e in by_trade.get(t, [])]
        ep_alerts = [a for t in members for a in alerts_by_trade.get(t, [])]
        times = [e.event_time for e in ep_events] + [a.alert_time for a in ep_alerts]
        quality: set[str] = set()
        for t in members:
            quality.update(_lifecycle_issues(by_trade.get(t, [])))
            quality.update(flags.get(t, set()))
        episode_id = stable_id("EP", *members)
        episode = Episode(
            episode_id=episode_id,
            trade_ids=tuple(members),
            alert_ids=tuple(sorted(a.alert_id for a in ep_alerts)),
            start=min(times),
            end=max(times),
            desks=tuple(sorted({e.desk for e in ep_events} | {a.desk for a in ep_alerts})),
            books=tuple(sorted({e.book for e in ep_events} | {a.book for a in ep_alerts})),
            instrument_ids=tuple(
                sorted({e.instrument_id for e in ep_events} | {a.instrument_id for a in ep_alerts})
            ),
            product_types=tuple(sorted({e.product_type for e in ep_events})),
            links=tuple(
                sorted(
                    (x for x in accepted if x.src in member_set),
                    key=lambda x: (x.src, x.dst, x.kind.value),
                )
            ),
            rejected_links=tuple(
                sorted(
                    (x for x in rejected if x.src in member_set or x.dst in member_set),
                    key=lambda x: (x.src, x.dst, x.kind.value),
                )
            ),
            quality_flags=tuple(sorted(quality)),
            linking_version=version,
        )
        episodes.append(episode)
        for t in members:
            episode_of_trade[t] = episode_id
        for a in ep_alerts:
            episode_of_alert[a.alert_id] = episode_id

    unresolved = _residue(by_trade, episode_of_trade, accepted, cfg)
    return LinkingResult(
        episodes=tuple(sorted(episodes, key=lambda e: (e.start, e.episode_id))),
        unresolved=unresolved,
        episode_of_trade=episode_of_trade,
        episode_of_alert=episode_of_alert,
        version=version,
    )


def _residue(
    by_trade: Mapping[str, list[TradeEvent]],
    episode_of_trade: Mapping[str, str],
    accepted: Sequence[TradeLink],
    cfg: Config,
) -> tuple[UnresolvedPair, ...]:
    """Cancelled trades with no rebook link, paired with economically identical new trades
    booked shortly after in another episode. Evidence of a *possible* relationship only."""
    window = timedelta(hours=cfg.integer("linking", "rebook_window_hours"))
    max_candidates = cfg.integer("linking", "residue_max_candidates")
    linked = {x.dst for x in accepted if x.kind is LinkKind.REBOOK_OF}
    linked |= {x.src for x in accepted if x.kind is LinkKind.REBOOK_OF}
    starts = {
        t: evs[0]
        for t, evs in by_trade.items()
        if evs[0].event_type is EventType.NEW and not any(e.original_trade_id for e in evs)
    }
    pairs: list[UnresolvedPair] = []
    for t in sorted(by_trade):
        evs = by_trade[t]
        cancel = next((e for e in evs if e.event_type is EventType.CANCEL), None)
        if cancel is None or t in linked:
            continue
        last = (
            [e for e in evs if e.event_type is not EventType.CANCEL][-1] if len(evs) > 1 else evs[0]
        )
        found: list[tuple[timedelta, str]] = []
        for other, first in starts.items():
            if other == t or episode_of_trade.get(other) == episode_of_trade.get(t):
                continue
            gap = first.event_time - cancel.event_time
            if not (timedelta(0) <= gap <= window):
                continue
            if (first.instrument_id, first.book, first.side, first.quantity) != (
                last.instrument_id,
                last.book,
                last.side,
                last.quantity,
            ):
                continue
            found.append((gap, other))
        for gap, other in sorted(found)[:max_candidates]:
            pairs.append(
                UnresolvedPair(
                    pair_id=stable_id("PAIR", t, other),
                    cancelled_trade=t,
                    candidate_trade=other,
                    episode_a=episode_of_trade[t],
                    episode_b=episode_of_trade[other],
                    features={
                        "same_instrument": "true",
                        "same_book": "true",
                        "same_side": "true",
                        "same_quantity": "true",
                        "minutes_after_cancel": str(int(gap.total_seconds() // 60)),
                        "candidate_source": by_trade[other][0].source,
                    },
                )
            )
    return tuple(pairs)
