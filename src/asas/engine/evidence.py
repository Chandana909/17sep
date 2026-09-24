"""Deterministic evidence functions: the facts behind every agent tool.

Each function reads the frozen snapshot and returns a bounded, typed view. Nothing returns
raw row sets; free text is delimited and flagged untrusted; person data requires entitlement.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from pydantic import BaseModel, ConfigDict

from asas.core.errors import PermissionDenied, ToolError
from asas.core.ids import content_hash
from asas.core.security import Principal
from asas.domain.models import (
    EventType,
    LabelQuality,
    LinkKind,
    LinkStatus,
    OutcomeLabel,
    RuleSpec,
    TradeEvent,
    TradeLink,
    UnresolvedPair,
)
from asas.engine.signals import period_end, period_start
from asas.engine.snapshot import Snapshot

_Q = Decimal("0.0001")
_PCT = Decimal(100)
_HOUR = Decimal(3600)
UNTRUSTED_OPEN = "<<<UNTRUSTED_TEXT>>>"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_TEXT>>>"


class View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def untrusted(text: str | None) -> str | None:
    """Delimit free text as data. Control characters are removed and fence markers broken so
    the text cannot close its own fence."""
    if text is None:
        return None
    cleaned = "".join(
        ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    cleaned = cleaned.replace("<<<", "< < <").replace(">>>", "> > >")
    return f"{UNTRUSTED_OPEN}\n{cleaned}\n{UNTRUSTED_CLOSE}"


def _hours(delta: timedelta) -> Decimal:
    return (Decimal(str(delta.total_seconds())) / _HOUR).quantize(_Q)


def hours_between(start: datetime, end: datetime) -> Decimal:
    return _hours(end - start)


def _pct(old: Decimal, new: Decimal) -> Decimal:
    return (abs(new - old) / abs(old) * _PCT).quantize(_Q)


def _bound(snap: Snapshot) -> int:
    return snap.cfg.integer("tools", "max_items")


def _episode(snap: Snapshot, episode_id: str) -> None:
    if episode_id not in snap.episodes_by_id:
        raise ToolError(f"unknown episode {episode_id}")


def _trade(snap: Snapshot, trade_id: str) -> tuple[TradeEvent, ...]:
    events = snap.events_by_trade.get(trade_id)
    if not events:
        raise ToolError(f"unknown trade {trade_id}")
    return events


# ---------------------------------------------------------------- views


class AlertView(View):
    alert_id: str
    rule_id: str
    subrule_id: str
    trade_id: str
    trade_version: int | None
    alert_time: datetime
    desk: str
    book: str
    instrument_id: str
    explanation_untrusted: str | None


class LinkView(View):
    src: str
    dst: str
    kind: str
    tier: str
    status: str
    reason: str


class EpisodeView(View):
    episode_id: str
    trade_ids: tuple[str, ...]
    alert_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    start: datetime
    end: datetime
    desk: str
    books: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    links: tuple[LinkView, ...]
    rejected_links: tuple[LinkView, ...]
    quality_flags: tuple[str, ...]
    signals: dict[str, str]
    score_total: Decimal
    band: str
    bucket: str
    outlier_signals: tuple[str, ...]
    unresolved_pairs: tuple[str, ...]


class VersionView(View):
    version: int
    event_type: str
    event_time: datetime
    record_time: datetime
    price: Decimal | None
    quantity: Decimal | None
    side: str | None
    notional_usd: Decimal | None
    book: str


class TradeView(View):
    trade_id: str
    desk: str
    book: str
    instrument_id: str
    product_type: str
    source: str
    original_trade_id: str | None
    urn_ref: str | None
    n_versions: int
    latest: VersionView


class TradeHistory(View):
    trade_id: str
    versions: tuple[VersionView, ...]


class VersionDiff(View):
    from_version: int
    to_version: int
    event_type: str
    price_change_pct: Decimal | None
    quantity_changed: bool
    side_changed: bool
    instrument_changed: bool
    book_changed: bool
    hours_after_previous: Decimal
    hours_after_booking: Decimal
    hours_to_period_end: Decimal
    crosses_period_end: bool
    restores_original_price: bool


class VersionComparison(View):
    trade_id: str
    diffs: tuple[VersionDiff, ...]


class SeqEvent(View):
    trade_id: str
    version: int
    event_type: str
    hours_from_start: Decimal
    price: Decimal | None
    quantity: Decimal | None
    side: str | None
    is_rebook: bool


class EventSequence(View):
    episode_id: str
    signature: str
    events: tuple[SeqEvent, ...]


class AlertBrief(View):
    alert_id: str
    rule_id: str
    trade_id: str
    alert_time: datetime


class RelatedAlerts(View):
    episode_id: str
    in_episode: tuple[AlertBrief, ...]
    nearby: tuple[AlertBrief, ...]


class EpisodeBrief(View):
    episode_id: str
    signature: str
    start: datetime
    desk: str
    book: str
    instrument_id: str


class RelatedEpisodes(View):
    episode_id: str
    nearby: tuple[EpisodeBrief, ...]
    unresolved: tuple[UnresolvedPair, ...]


class TraderBaseline(View):
    episode_id: str
    trader_ref: str
    trades: int
    amend_rate_pct: Decimal
    cancel_rate_pct: Decimal
    desk_amend_rate_pct: Decimal
    desk_cancel_rate_pct: Decimal
    raise_only: bool = True


class PeerComparison(View):
    episode_id: str
    signal: str
    available: bool
    value: Decimal | None
    percentile: Decimal | None
    robust_z: Decimal | None
    peer_level: str
    peer_n: int


class Recurrence(View):
    episode_id: str
    signature: str
    book_signature_recurrence: Decimal
    instrument_signature_recurrence: Decimal
    book_rebook_episodes: int
    window_days: int


class PriorOutcomes(View):
    episode_id: str
    signature: str
    book: str
    prior_episodes: int
    curated_cleared: int
    curated_escalated: int
    raw_ignored: int


class RuleView(View):
    rule_id: str
    source: str
    severity: str
    subrules: tuple[RuleSpec, ...]


class TradeComparison(View):
    trade_a: str
    trade_b: str
    a_cancelled: bool
    same_instrument: bool
    same_book: bool
    same_side: bool
    quantity_diff_pct: Decimal | None
    price_diff_pct: Decimal | None
    minutes_after_cancel: Decimal | None
    b_has_original_link: bool
    b_source: str


# ---------------------------------------------------------------- functions


def alert_view(snap: Snapshot, alert_id: str) -> AlertView:
    a = snap.alerts_by_id.get(alert_id)
    if a is None:
        raise ToolError(f"unknown alert {alert_id}")
    return AlertView(
        alert_id=a.alert_id,
        rule_id=a.rule_id,
        subrule_id=a.subrule_id,
        trade_id=a.trade_id,
        trade_version=a.trade_version,
        alert_time=a.alert_time,
        desk=a.desk,
        book=a.book,
        instrument_id=a.instrument_id,
        explanation_untrusted=untrusted(a.explanation),
    )


def _link_view(link: TradeLink) -> LinkView:
    return LinkView(
        src=link.src,
        dst=link.dst,
        kind=link.kind.value,
        tier=link.tier.value,
        status=link.status.value,
        reason=link.reason,
    )


def episode_view(snap: Snapshot, episode_id: str) -> EpisodeView:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    sig = snap.signal_set.signals[episode_id]
    score = snap.scores[episode_id]
    signals = {k: str(v) for k, v in sig.numeric.items()}
    signals.update({k: str(v).lower() for k, v in sig.flags.items()})
    signals.update(sig.labels)
    pairs = tuple(
        p.pair_id for p in snap.linking.unresolved if episode_id in (p.episode_a, p.episode_b)
    )
    return EpisodeView(
        episode_id=ep.episode_id,
        trade_ids=ep.trade_ids,
        alert_ids=ep.alert_ids,
        rule_ids=snap.episode_rules(episode_id),
        start=ep.start,
        end=ep.end,
        desk=ep.desk,
        books=ep.books,
        instrument_ids=ep.instrument_ids,
        links=tuple(_link_view(x) for x in ep.links),
        rejected_links=tuple(_link_view(x) for x in ep.rejected_links),
        quality_flags=ep.quality_flags,
        signals=signals,
        score_total=score.total,
        band=score.band,
        bucket=score.bucket,
        outlier_signals=tuple(o.signal for o in score.outliers),
        unresolved_pairs=pairs,
    )


def _version(e: TradeEvent) -> VersionView:
    return VersionView(
        version=e.version,
        event_type=e.event_type.value,
        event_time=e.event_time,
        record_time=e.record_time,
        price=e.price,
        quantity=e.quantity,
        side=e.side.value if e.side else None,
        notional_usd=e.notional_usd,
        book=e.book,
    )


def trade_view(snap: Snapshot, trade_id: str) -> TradeView:
    events = _trade(snap, trade_id)
    first, last = events[0], events[-1]
    return TradeView(
        trade_id=trade_id,
        desk=first.desk,
        book=last.book,
        instrument_id=first.instrument_id,
        product_type=first.product_type,
        source=first.source,
        original_trade_id=next((e.original_trade_id for e in events if e.original_trade_id), None),
        urn_ref=first.urn_ref,
        n_versions=len(events),
        latest=_version(last),
    )


def trade_history(snap: Snapshot, trade_id: str) -> TradeHistory:
    events = _trade(snap, trade_id)
    return TradeHistory(
        trade_id=trade_id, versions=tuple(_version(e) for e in events[: _bound(snap)])
    )


def compare_versions(snap: Snapshot, trade_id: str) -> VersionComparison:
    events = _trade(snap, trade_id)
    diffs: list[VersionDiff] = []
    booking = events[0]
    original_price = booking.price
    for prev, cur in pairwise(events):
        change = _pct(prev.price, cur.price) if prev.price and cur.price is not None else None
        restores = bool(
            original_price
            and cur.price is not None
            and prev.price != original_price
            and cur.event_type is EventType.AMEND
            and _pct(original_price, cur.price)
            <= snap.cfg.decimal("signals", "round_trip_tolerance_pct")
        )
        diffs.append(
            VersionDiff(
                from_version=prev.version,
                to_version=cur.version,
                event_type=cur.event_type.value,
                price_change_pct=change if cur.event_type is not EventType.CANCEL else None,
                quantity_changed=cur.event_type is not EventType.CANCEL
                and prev.quantity != cur.quantity,
                side_changed=prev.side != cur.side,
                instrument_changed=prev.instrument_id != cur.instrument_id,
                book_changed=prev.book != cur.book,
                hours_after_previous=_hours(cur.event_time - prev.event_time),
                hours_after_booking=_hours(cur.event_time - booking.event_time),
                hours_to_period_end=_hours(period_end(cur.event_time) - cur.event_time),
                crosses_period_end=period_start(cur.event_time) != period_start(prev.event_time),
                restores_original_price=restores,
            )
        )
    return VersionComparison(trade_id=trade_id, diffs=tuple(diffs[: _bound(snap)]))


def event_sequence(snap: Snapshot, episode_id: str) -> EventSequence:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    rebooks = {x.src for x in ep.links if x.kind in (LinkKind.REBOOK_OF, LinkKind.AGENT_LINK)}
    events = sorted(
        (e for t in ep.trade_ids for e in snap.events_by_trade.get(t, ())),
        key=lambda e: (e.event_time, e.trade_id, e.version),
    )
    start = events[0].event_time if events else ep.start
    return EventSequence(
        episode_id=episode_id,
        signature=snap.signal_set.signals[episode_id].labels["sequence_signature"],
        events=tuple(
            SeqEvent(
                trade_id=e.trade_id,
                version=e.version,
                event_type=e.event_type.value,
                hours_from_start=_hours(e.event_time - start),
                price=e.price,
                quantity=e.quantity,
                side=e.side.value if e.side else None,
                is_rebook=e.trade_id in rebooks and e.event_type is EventType.NEW,
            )
            for e in events[: _bound(snap)]
        ),
    )


def related_alerts(snap: Snapshot, episode_id: str) -> RelatedAlerts:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    window = timedelta(hours=snap.cfg.integer("tools", "related_window_hours"))

    def brief(alert_id: str) -> AlertBrief:
        a = snap.alerts_by_id[alert_id]
        return AlertBrief(
            alert_id=a.alert_id, rule_id=a.rule_id, trade_id=a.trade_id, alert_time=a.alert_time
        )

    nearby = sorted(
        a.alert_id
        for a in snap.alerts_by_id.values()
        if a.alert_id not in ep.alert_ids
        and (a.instrument_id in ep.instrument_ids or a.book in ep.books)
        and ep.start - window <= a.alert_time <= ep.end + window
    )
    return RelatedAlerts(
        episode_id=episode_id,
        in_episode=tuple(brief(a) for a in ep.alert_ids),
        nearby=tuple(brief(a) for a in nearby[: _bound(snap)]),
    )


def related_episodes(snap: Snapshot, episode_id: str) -> RelatedEpisodes:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    window = timedelta(hours=snap.cfg.integer("tools", "related_window_hours"))
    nearby: list[EpisodeBrief] = []
    for other in snap.episodes:
        if other.episode_id == episode_id:
            continue
        if not (set(other.instrument_ids) & set(ep.instrument_ids)):
            continue
        if not (ep.start - window <= other.start <= ep.end + window):
            continue
        s = snap.signal_set.signals[other.episode_id]
        nearby.append(
            EpisodeBrief(
                episode_id=other.episode_id,
                signature=s.labels["sequence_signature"],
                start=other.start,
                desk=other.desk,
                book=s.labels["book"],
                instrument_id=s.labels["instrument_id"],
            )
        )
    unresolved = tuple(
        p for p in snap.linking.unresolved if episode_id in (p.episode_a, p.episode_b)
    )
    return RelatedEpisodes(
        episode_id=episode_id,
        nearby=tuple(nearby[: _bound(snap)]),
        unresolved=unresolved[: _bound(snap)],
    )


def trader_baseline(snap: Snapshot, episode_id: str, principal: Principal) -> TraderBaseline:
    if not principal.person_data:
        raise PermissionDenied("trader baseline requires person-data entitlement")
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    person = next(
        (snap.persons_by_trade[t] for t in ep.trade_ids if t in snap.persons_by_trade), None
    )
    if person is None:
        raise ToolError("no trader recorded for this episode")
    mine = [t for t, p in snap.persons_by_trade.items() if p.trader_id == person.trader_id]
    desk = [t for t, evs in snap.events_by_trade.items() if evs[0].desk == ep.desk]

    def rates(trades: list[str]) -> tuple[Decimal, Decimal]:
        if not trades:
            return Decimal(), Decimal()
        amended = sum(
            any(e.event_type is EventType.AMEND for e in snap.events_by_trade[t]) for t in trades
        )
        cancelled = sum(
            any(e.event_type is EventType.CANCEL for e in snap.events_by_trade[t]) for t in trades
        )
        n = Decimal(len(trades))
        return (Decimal(amended) / n * _PCT).quantize(_Q), (Decimal(cancelled) / n * _PCT).quantize(
            _Q
        )

    amend, cancel = rates(mine)
    desk_amend, desk_cancel = rates(desk)
    return TraderBaseline(
        episode_id=episode_id,
        trader_ref=content_hash(person.trader_id)[:12],
        trades=len(mine),
        amend_rate_pct=amend,
        cancel_rate_pct=cancel,
        desk_amend_rate_pct=desk_amend,
        desk_cancel_rate_pct=desk_cancel,
    )


def peer_comparison(snap: Snapshot, episode_id: str, signal: str) -> PeerComparison:
    _episode(snap, episode_id)
    s = snap.signal_set.signals[episode_id]
    baseline = snap.signal_set.baseline_for(s.eval_time)
    evidence = baseline.compare(s, signal, snap.cfg) if baseline else None
    if evidence is None:
        return PeerComparison(
            episode_id=episode_id,
            signal=signal,
            available=False,
            value=s.numeric.get(signal),
            percentile=None,
            robust_z=None,
            peer_level="none",
            peer_n=0,
        )
    return PeerComparison(
        episode_id=episode_id,
        signal=signal,
        available=True,
        value=evidence.value,
        percentile=evidence.percentile,
        robust_z=evidence.robust_z,
        peer_level=evidence.peer_level,
        peer_n=evidence.peer_n,
    )


def recurrence(snap: Snapshot, episode_id: str) -> Recurrence:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    s = snap.signal_set.signals[episode_id]
    days = snap.cfg.integer("signals", "recurrence_window_days")
    since = ep.start - timedelta(days=days)
    book = s.labels["book"]
    rebook_eps = sum(
        1
        for other in snap.episodes
        if other.episode_id != episode_id
        and since <= other.end < ep.start
        and snap.signal_set.signals[other.episode_id].labels["book"] == book
        and snap.signal_set.signals[other.episode_id].flags.get("has_rebook_link", False)
    )
    return Recurrence(
        episode_id=episode_id,
        signature=s.labels["sequence_signature"],
        book_signature_recurrence=s.numeric["book_signature_recurrence"],
        instrument_signature_recurrence=s.numeric["instrument_signature_recurrence"],
        book_rebook_episodes=rebook_eps,
        window_days=days,
    )


def prior_outcomes(snap: Snapshot, episode_id: str) -> PriorOutcomes:
    _episode(snap, episode_id)
    ep = snap.episodes_by_id[episode_id]
    s = snap.signal_set.signals[episode_id]
    sig = s.labels["sequence_signature"]
    book = s.labels["book"]
    since = ep.start - timedelta(days=snap.cfg.integer("hypotheses", "context_lookback_days"))
    cleared = escalated = raw = prior = 0
    for other in snap.episodes:
        if not (since <= other.end < ep.start):
            continue
        labels = snap.signal_set.signals[other.episode_id].labels
        if labels["book"] != book or labels["sequence_signature"] != sig:
            continue
        prior += 1
        for a in other.alert_ids:
            for o in snap.outcomes_by_alert.get(a, ()):
                if o.decided_at >= snap.as_of:
                    continue
                if o.quality is not LabelQuality.CURATED:
                    raw += 1
                elif o.label is OutcomeLabel.ESCALATED:
                    escalated += 1
                else:
                    cleared += 1
    return PriorOutcomes(
        episode_id=episode_id,
        signature=sig,
        book=book,
        prior_episodes=prior,
        curated_cleared=cleared,
        curated_escalated=escalated,
        raw_ignored=raw,
    )


def rule_view(snap: Snapshot, rule_id: str) -> RuleView:
    subrules = tuple(r for r in snap.policy.ruleset.rules if r.rule_id == rule_id)
    severities = snap.cfg.section("scoring", "rule_severity")
    severity = str(severities.get(rule_id, snap.cfg.string("scoring", "unknown_rule_severity")))
    known = {a.rule_id for a in snap.alerts_by_id.values()}
    if not subrules and rule_id not in known:
        raise ToolError(f"unknown rule {rule_id}")
    return RuleView(
        rule_id=rule_id,
        source="production" if subrules else "external",
        severity=severity,
        subrules=subrules,
    )


def compare_trades(snap: Snapshot, trade_a: str, trade_b: str) -> TradeComparison:
    a_events, b_events = _trade(snap, trade_a), _trade(snap, trade_b)
    cancel = next((e for e in a_events if e.event_type is EventType.CANCEL), None)
    live_a = [e for e in a_events if e.event_type is not EventType.CANCEL]
    a_last = live_a[-1] if live_a else a_events[0]
    b_first = b_events[0]
    qty = (
        _pct(a_last.quantity, b_first.quantity)
        if a_last.quantity and b_first.quantity is not None
        else None
    )
    price = (
        _pct(a_last.price, b_first.price) if a_last.price and b_first.price is not None else None
    )
    minutes = (
        (
            Decimal(str((b_first.event_time - cancel.event_time).total_seconds())) / Decimal(60)
        ).quantize(_Q)
        if cancel
        else None
    )
    return TradeComparison(
        trade_a=trade_a,
        trade_b=trade_b,
        a_cancelled=cancel is not None,
        same_instrument=a_last.instrument_id == b_first.instrument_id,
        same_book=a_last.book == b_first.book,
        same_side=a_last.side == b_first.side,
        quantity_diff_pct=qty,
        price_diff_pct=price,
        minutes_after_cancel=minutes,
        b_has_original_link=any(e.original_trade_id for e in b_events),
        b_source=b_first.source,
    )


def verified_link_ids(snap: Snapshot) -> set[tuple[str, str]]:
    return {
        (p.trade_a, p.trade_b)
        for p in snap.link_proposals
        if p.status in (LinkStatus.VERIFIED, LinkStatus.CANONICAL)
    }


def facts(view: BaseModel, max_nested: int = 40) -> dict[str, str]:
    """Flatten a view into display facts: scalars, one level of nested scalars, and sizes of
    collections (bounded)."""
    out: dict[str, str] = {}
    for name, value in view.model_dump(mode="json").items():
        if isinstance(value, dict):
            out[f"{name}.count"] = str(len(value))
            for key, inner in list(value.items())[:max_nested]:
                if isinstance(inner, str | int | float | bool):
                    out[f"{name}.{key}"] = str(inner)
        elif isinstance(value, list | tuple):
            out[f"{name}.count"] = str(len(value))
        elif value is not None:
            out[name] = str(value)
    return out


VIEW_TYPES: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        AlertView,
        EpisodeView,
        TradeView,
        TradeHistory,
        VersionComparison,
        EventSequence,
        RelatedAlerts,
        RelatedEpisodes,
        TraderBaseline,
        PeerComparison,
        Recurrence,
        PriorOutcomes,
        RuleView,
        TradeComparison,
    )
}
