"""Read-only access to SCP/CAL snapshots with point-in-time filtering (rails 1, 10).

The protocol exposes no mutating operations. A production adapter must connect with a
SELECT-only role and push `record_time <= :as_of` into every query (see docs/SAD.md)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from asas.models import Alert, AlertAnnex, PastCase, RfiEvent, TradeEvent


class HasRecordTime(Protocol):
    @property
    def record_time(self) -> datetime: ...


R = TypeVar("R", bound=HasRecordTime)


class PointInTimeViolation(RuntimeError):
    pass


def visible(records: Iterable[R], as_of: datetime) -> tuple[R, ...]:
    return tuple(r for r in records if r.record_time <= as_of)


def decided_before(cases: Iterable[PastCase], as_of: datetime) -> tuple[PastCase, ...]:
    return tuple(c for c in cases if c.decided_at < as_of)


def assert_pit(records: Iterable[HasRecordTime], as_of: datetime) -> None:
    for r in records:
        if r.record_time > as_of:
            raise PointInTimeViolation(f"record_time {r.record_time} > as_of {as_of}")


class ReadOnlySource(Protocol):
    def alerts(self, as_of: datetime) -> tuple[Alert, ...]: ...
    def annexes(self, as_of: datetime) -> tuple[AlertAnnex, ...]: ...
    def trade_events(self, as_of: datetime) -> tuple[TradeEvent, ...]: ...
    def rfi_events(self, as_of: datetime) -> tuple[RfiEvent, ...]: ...
    def past_cases(self, as_of: datetime) -> tuple[PastCase, ...]: ...


@dataclass(frozen=True)
class InMemorySource:
    all_alerts: tuple[Alert, ...] = ()
    all_annexes: tuple[AlertAnnex, ...] = ()
    all_trade_events: tuple[TradeEvent, ...] = ()
    all_rfi_events: tuple[RfiEvent, ...] = ()
    all_past_cases: tuple[PastCase, ...] = ()

    def alerts(self, as_of: datetime) -> tuple[Alert, ...]:
        return visible(self.all_alerts, as_of)

    def annexes(self, as_of: datetime) -> tuple[AlertAnnex, ...]:
        return visible(self.all_annexes, as_of)

    def trade_events(self, as_of: datetime) -> tuple[TradeEvent, ...]:
        return visible(self.all_trade_events, as_of)

    def rfi_events(self, as_of: datetime) -> tuple[RfiEvent, ...]:
        return visible(self.all_rfi_events, as_of)

    def past_cases(self, as_of: datetime) -> tuple[PastCase, ...]:
        return decided_before(self.all_past_cases, as_of)
