"""Unit tests for decision rails: fail-safe config, history, no suppression, PIT."""

from __future__ import annotations

import pytest

from asas import run
from asas.models import Treatment
from asas.sources import PointInTimeViolation, assert_pit
from factories import AS_OF, alert, cfg, corrected_trade, past, source, two_corrected_trades


@pytest.mark.parametrize(
    "path",
    [
        ("categories",),
        ("claims", "PRICE_CORRECTION"),
        ("verification", "price_correction_max_rel_change"),
        ("evidence", "required", "PRICE_CORRECTION"),
        ("history", "adverse_rate_threshold"),
        ("priority", "weights", "notional"),
        ("cohorts", "min_size"),
        ("linking", "window_hours"),
    ],
)
def test_missing_decision_config_fails_safe(path: tuple[str, ...]) -> None:
    result = run(two_corrected_trades(), cfg().without(*path), AS_OF)
    assert result.cohorts == ()
    for case in result.cases:
        assert case.treatment is Treatment.INDIVIDUAL_REVIEW
        assert any(r.startswith("CONFIG_MISSING:") for r in case.reasons)


def test_malformed_config_value_fails_safe() -> None:
    bad = cfg().with_value("not-a-number", "verification", "price_correction_max_rel_change")
    result = run(two_corrected_trades(), bad, AS_OF)
    assert all(c.treatment is Treatment.INDIVIDUAL_REVIEW for c in result.cases)


@pytest.mark.parametrize(("lo", "hi"), [(0, 5), (3, 2)])
def test_inconsistent_cohort_sizes_fail_safe(lo: int, hi: int) -> None:
    bad = cfg().with_value(lo, "cohorts", "min_size").with_value(hi, "cohorts", "max_size")
    result = run(two_corrected_trades(), bad, AS_OF)
    assert result.cohorts == ()
    assert all(c.treatment is Treatment.INDIVIDUAL_REVIEW for c in result.cases)


def test_adverse_history_forces_individual_review() -> None:
    src = two_corrected_trades()
    history = [past(f"P{i}", "ESCALATED" if i < 2 else "CLEARED", -100 - i) for i in range(5)]
    result = run(source(src.all_alerts, src.all_trade_events, pasts=history), cfg(), AS_OF)
    assert all("HISTORY_ADVERSE_COMPARABLES" in c.reasons for c in result.cases)


def test_benign_history_never_makes_a_case_bulk() -> None:
    a, e = corrected_trade("T1", "A1")
    blocked = alert("A2", trade="T2", text=None)
    history = [past(f"P{i}", "CLEARED", -10 - i) for i in range(50)]
    base = run(source([a, blocked], e), cfg(), AS_OF)
    with_history = run(source([a, blocked], e, pasts=history), cfg(), AS_OF)
    assert [c.treatment for c in base.cases] == [c.treatment for c in with_history.cases]


def test_history_uses_only_cases_decided_before_as_of() -> None:
    src = two_corrected_trades()
    future = [past(f"P{i}", "ESCALATED", 200 + i) for i in range(10)]  # decided_at >= as_of
    result = run(source(src.all_alerts, src.all_trade_events, pasts=future), cfg(), AS_OF)
    assert all(c.history_comparable == 0 for c in result.cases)


def test_no_suppression_every_visible_alert_is_in_review() -> None:
    src = two_corrected_trades()
    result = run(
        source([*src.all_alerts, alert("A3", trade="TX")], src.all_trade_events), cfg(), AS_OF
    )
    alert_ids = {a for c in result.cases for a in c.episode.alert_ids}
    assert alert_ids == {"A1", "A2", "A3"}
    cohort_ids = {cid for ch in result.cohorts for cid in ch.case_ids}
    queue = set(result.individual_queue)
    assert cohort_ids | queue == {c.case_id for c in result.cases}
    assert not cohort_ids & queue


def test_cohorts_split_at_max_size_and_remainder_goes_individual() -> None:
    alerts, events = [], []
    for i in range(5):
        a, e = corrected_trade(f"T{i}", f"A{i}")
        alerts.append(a)
        events += e
    small = cfg().with_value(2, "cohorts", "max_size")
    result = run(source(alerts, events), small, AS_OF)
    assert sorted(len(c.case_ids) for c in result.cohorts) == [2, 2]
    assert len(result.individual_queue) == 1


def test_assert_pit_detects_future_record() -> None:
    with pytest.raises(PointInTimeViolation):
        assert_pit([alert("A1", rec=300)], AS_OF)


def test_future_records_are_invisible() -> None:
    src = two_corrected_trades()
    late = alert("A9", trade="T1", h=3, rec=500)
    result = run(source([*src.all_alerts, late], src.all_trade_events), cfg(), AS_OF)
    assert "A9" not in {a for c in result.cases for a in c.episode.alert_ids}


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValueError):
        run(two_corrected_trades(), cfg(), AS_OF.replace(tzinfo=None))
