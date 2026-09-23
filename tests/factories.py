from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from asas.config import Config, load_config
from asas.models import (
    Alert,
    AlertAnnex,
    EventType,
    PastCase,
    RfiAction,
    RfiEvent,
    TradeEvent,
)
from asas.sources import InMemorySource

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 1, 5, 9, tzinfo=UTC)
AS_OF = T0 + timedelta(hours=200)


def ts(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def cfg() -> Config:
    return load_config(ROOT / "config" / "asas.v1.toml")


def alert(
    aid: str,
    trade: str = "T1",
    atype: str = "PRICE_OFF_MARKET",
    h: float = 1,
    rec: float | None = None,
    text: str | None = "Fat-finger price, corrected by amendment.",
    instrument: str = "I1",
    book: str = "B1",
) -> Alert:
    return Alert(aid, atype, trade, instrument, book, ts(h), ts(h if rec is None else rec), text)


def event(
    trade: str = "T1",
    etype: str = "NEW",
    h: float = 0,
    price: str | None = "100",
    qty: str | None = "10",
    instrument: str = "I1",
    book: str = "B1",
    rec: float | None = None,
) -> TradeEvent:
    return TradeEvent(
        trade,
        EventType(etype),
        ts(h),
        ts(h if rec is None else rec),
        instrument,
        book,
        None if price is None else Decimal(price),
        None if qty is None else Decimal(qty),
    )


def corrected_trade(trade: str, aid: str) -> tuple[Alert, list[TradeEvent]]:
    """Archetype: price off-market, amended within tolerance."""
    return alert(aid, trade=trade), [
        event(trade, "NEW", 0, "100"),
        event(trade, "AMEND", 2, "101"),
    ]


def rfi(aid: str, action: str, h: float) -> RfiEvent:
    return RfiEvent(aid, RfiAction(action), ts(h))


def past(cid: str, outcome: str, h: float, category: str = "PRICE_CORRECTION") -> PastCase:
    return PastCase(cid, category, outcome, ts(h))


def source(
    alerts: list[Alert] | tuple[Alert, ...] = (),
    events: list[TradeEvent] | tuple[TradeEvent, ...] = (),
    rfis: list[RfiEvent] | tuple[RfiEvent, ...] = (),
    pasts: list[PastCase] | tuple[PastCase, ...] = (),
    annexes: list[AlertAnnex] | tuple[AlertAnnex, ...] = (),
) -> InMemorySource:
    return InMemorySource(tuple(alerts), tuple(annexes), tuple(events), tuple(rfis), tuple(pasts))


def two_corrected_trades() -> InMemorySource:
    a1, e1 = corrected_trade("T1", "A1")
    a2, e2 = corrected_trade("T2", "A2")
    return source([a1, a2], e1 + e2)
