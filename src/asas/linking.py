"""Alerts -> validated lifecycle episodes. Pure and order-independent."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import timedelta

from asas.ids import stable_id
from asas.models import Alert, Episode, EventType, TradeEvent


def order_events(events: Iterable[TradeEvent]) -> tuple[TradeEvent, ...]:
    return tuple(
        sorted(
            events,
            key=lambda e: (e.event_time, e.record_time, e.event_type.value, str(e.price)),
        )
    )


def validate_lifecycle(events: Sequence[TradeEvent]) -> tuple[str, ...]:
    if not events:
        return ("NO_TRADE_EVENTS",)
    issues: list[str] = []
    types = [e.event_type for e in events]
    if types[0] is not EventType.NEW:
        issues.append("FIRST_EVENT_NOT_NEW")
    if types.count(EventType.NEW) > 1:
        issues.append("MULTIPLE_NEW_EVENTS")
    if EventType.CANCEL in types and types.index(EventType.CANCEL) != len(types) - 1:
        issues.append("EVENTS_AFTER_CANCEL")
    if len({e.instrument_id for e in events}) > 1:
        issues.append("INSTRUMENT_CHANGED")
    return tuple(issues)


def link_episodes(
    alerts: Iterable[Alert], events: Iterable[TradeEvent], window: timedelta
) -> tuple[Episode, ...]:
    events_by_trade: dict[str, list[TradeEvent]] = defaultdict(list)
    for event in events:
        events_by_trade[event.trade_id].append(event)
    alerts_by_trade: dict[str, list[Alert]] = defaultdict(list)
    for alert in alerts:
        alerts_by_trade[alert.trade_id].append(alert)

    episodes: list[Episode] = []
    for trade_id, trade_alerts in alerts_by_trade.items():
        trade_events = order_events(events_by_trade.get(trade_id, ()))
        issues = validate_lifecycle(trade_events)
        ordered = sorted(trade_alerts, key=lambda a: (a.alert_time, a.alert_id))
        groups: list[list[Alert]] = [[ordered[0]]]
        for alert in ordered[1:]:
            if alert.alert_time - groups[-1][-1].alert_time > window:
                groups.append([alert])
            else:
                groups[-1].append(alert)
        for group in groups:
            ids = tuple(a.alert_id for a in group)
            episodes.append(
                Episode(
                    episode_id=stable_id("EP", trade_id, *ids),
                    trade_id=trade_id,
                    alert_ids=ids,
                    start=group[0].alert_time,
                    end=group[-1].alert_time,
                    events=trade_events,
                    lifecycle_issues=issues,
                )
            )
    return tuple(sorted(episodes, key=lambda e: e.episode_id))
