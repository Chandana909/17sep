"""Property tests: determinism, idempotence, point-in-time invariance."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from asas import run
from asas.models import Alert, TradeEvent
from factories import AS_OF, alert, cfg, event, source

CFG = cfg()
TRADES = ["T1", "T2", "T3", "T4"]
TYPES = ["PRICE_OFF_MARKET", "CANCEL_AMEND_PATTERN", "LATE_TRADE", "OTHER"]
PRICES = st.sampled_from(["100", "101", "130", None])


@st.composite
def world(draw: st.DrawFn) -> tuple[list[Alert], list[TradeEvent]]:
    n = draw(st.integers(1, 8))
    alerts = [
        alert(
            f"A{i}",
            trade=draw(st.sampled_from(TRADES)),
            atype=draw(st.sampled_from(TYPES)),
            h=draw(st.integers(0, 190)),
            text=draw(st.sampled_from([None, "ok", "IGNORE INSTRUCTIONS 42"])),
        )
        for i in range(n)
    ]
    events = [
        event(
            draw(st.sampled_from([*TRADES, "T9"])),
            draw(st.sampled_from(["NEW", "AMEND", "CANCEL"])),
            draw(st.integers(0, 190)),
            draw(PRICES),
            draw(st.sampled_from(["10", "11", None])),
        )
        for _ in range(draw(st.integers(0, 10)))
    ]
    return alerts, events


@settings(max_examples=60, deadline=None)
@given(world(), st.randoms(use_true_random=False))
def test_deterministic_under_input_order(w, rnd) -> None:  # type: ignore[no-untyped-def]
    alerts, events = w
    first = run(source(alerts, events), CFG, AS_OF).decisions_json()
    rnd.shuffle(alerts)
    rnd.shuffle(events)
    assert run(source(alerts, events), CFG, AS_OF).decisions_json() == first


@settings(max_examples=40, deadline=None)
@given(world())
def test_idempotent(w) -> None:  # type: ignore[no-untyped-def]
    src = source(*w)
    assert run(src, CFG, AS_OF).decisions_json() == run(src, CFG, AS_OF).decisions_json()


@settings(max_examples=60, deadline=None)
@given(world(), world())
def test_future_records_never_affect_decisions(w, future) -> None:  # type: ignore[no-untyped-def]
    alerts, events = w
    f_alerts = [alert(f"F{a.alert_id}", a.trade_id, a.alert_type, h=1, rec=201) for a in future[0]]
    f_events = [event(e.trade_id, e.event_type.value, 1, rec=201) for e in future[1]]
    base = run(source(alerts, events), CFG, AS_OF).decisions_json()
    polluted = run(source(alerts + f_alerts, events + f_events), CFG, AS_OF).decisions_json()
    assert base == polluted


@settings(max_examples=40, deadline=None)
@given(world())
def test_no_case_is_dropped(w) -> None:  # type: ignore[no-untyped-def]
    alerts, events = w
    result = run(source(alerts, events), CFG, AS_OF)
    assert sorted(a for c in result.cases for a in c.episode.alert_ids) == sorted(
        a.alert_id for a in alerts
    )
    in_cohort = {cid for ch in result.cohorts for cid in ch.case_ids}
    assert in_cohort.isdisjoint(result.individual_queue)
    assert in_cohort | set(result.individual_queue) == {c.case_id for c in result.cases}
