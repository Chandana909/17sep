"""Deterministic engine: linking guards, signals, scoring, rules DSL, hypotheses, decisions."""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from asas.core.config import Config
from asas.core.security import SYSTEM
from asas.data.ingest import SourceBundle
from asas.data.synthetic import SyntheticDataset
from asas.domain.models import (
    Alert,
    BulkPolicy,
    BulkScope,
    Condition,
    EventType,
    HypothesisClass,
    Op,
    PolicyBundle,
    RuleSet,
    RuleSpec,
    Side,
    TradeEvent,
)
from asas.engine.hypotheses import assess
from asas.engine.linking import build_episodes
from asas.engine.rules import evaluate_rule, load_ruleset, validate_rule
from asas.engine.signals import _percentile
from asas.engine.snapshot import build_snapshot
from asas.services.demo import ruleset_path

T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)


def ev(
    trade: str,
    version: int,
    etype: str,
    hours: float,
    *,
    price: str = "100",
    qty: str = "10",
    instrument: str = "I1",
    book: str = "B1",
    urn: str | None = None,
    original: str | None = None,
    alt: str | None = None,
) -> TradeEvent:
    at = T0 + timedelta(hours=hours)
    return TradeEvent(
        trade_id=trade,
        version=version,
        event_type=EventType(etype),
        event_time=at,
        record_time=at + timedelta(minutes=1),
        book=book,
        desk="EQ",
        instrument_id=instrument,
        product_type="EQUITY",
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal(price),
        currency="USD",
        notional_usd=Decimal(qty) * Decimal(price),
        original_trade_id=original,
        alternate_trade_id=alt,
        urn_ref=urn,
        source="CAL",
    )


def alert(aid: str, trade: str, rule: str, hours: float) -> Alert:
    at = T0 + timedelta(hours=hours)
    return Alert(
        alert_id=aid,
        rule_id=rule,
        subrule_id=f"{rule}.1",
        alert_time=at,
        record_time=at,
        trade_id=trade,
        trade_version=1,
        book="B1",
        desk="EQ",
        instrument_id="I1",
        explanation=None,
    )


def policy(cfg: Config) -> PolicyBundle:
    return PolicyBundle(
        bundle_id="B-TEST",
        version=1,
        parent_id=None,
        ruleset=load_ruleset(ruleset_path(), cfg),
        bulk_policy=BulkPolicy(
            allowed=(BulkScope(hypothesis_type="CANCEL_REBOOK_CORRECTION", desk="*"),)
        ),
        created_at=T0,
        created_by="test",
        source_candidate=None,
        notes="",
    )


# ---------------------------------------------------------------- linking


def test_multiple_alerts_collapse_into_one_episode(cfg: Config) -> None:
    events = [ev("T1", 1, "NEW", 0), ev("T1", 2, "CANCEL", 2), ev("T2", 1, "NEW", 3, original="T1")]
    alerts = [
        alert("A1", "T1", "R200", 2),
        alert("A2", "T2", "R400", 3),
        alert("A3", "T1", "R400", 0),
    ]
    result = build_episodes(events, alerts, cfg)
    assert len(result.episodes) == 1
    episode = result.episodes[0]
    assert episode.alert_ids == ("A1", "A2", "A3") and episode.trade_ids == ("T1", "T2")
    assert episode.links[0].reason == "ORIGINAL_TRADE_ID" and episode.links[0].tier.value == "S"


def test_degenerate_and_hub_keys_never_link(cfg: Config) -> None:
    events = [ev(f"T{i}", 1, "NEW", i, urn="UNKNOWN") for i in range(3)]
    events += [
        ev(f"H{i}", 1, "NEW", i, urn="URN-HUB")
        for i in range(cfg.integer("linking", "max_key_fanout") + 1)
    ]
    result = build_episodes(events, [], cfg)
    assert all(len(e.trade_ids) == 1 for e in result.episodes)
    flags = Counter(f for e in result.episodes for f in e.quality_flags)
    assert flags["DEGENERATE_URN_REF"] == 3 and flags["HUB_DEMOTED_URN_REF"] == 5


def test_medium_links_respect_instrument_and_gap_guards(cfg: Config) -> None:
    events = [
        ev("T1", 1, "NEW", 0, urn="URN-1"),
        ev("T2", 1, "NEW", 1, urn="URN-1"),
        ev("T3", 1, "NEW", 0, urn="URN-2"),
        ev("T4", 1, "NEW", 1, urn="URN-2", instrument="I2"),
        ev("T5", 1, "NEW", 0, urn="URN-3"),
        ev("T6", 1, "NEW", 100, urn="URN-3"),
    ]
    result = build_episodes(events, [], cfg)
    groups = {e.trade_ids for e in result.episodes}
    assert ("T1", "T2") in groups and ("T3",) in groups and ("T5",) in groups
    reasons = {x.reason for e in result.episodes for x in e.rejected_links}
    assert {"CROSS_INSTRUMENT", "OVER_GAP"} <= reasons


def test_untagged_rebook_becomes_residue_not_a_link(cfg: Config) -> None:
    events = [ev("T1", 1, "NEW", 0), ev("T1", 2, "CANCEL", 2), ev("T2", 1, "NEW", 2.5)]
    result = build_episodes(events, [], cfg)
    assert len(result.episodes) == 2
    (pair,) = result.unresolved
    assert (pair.cancelled_trade, pair.candidate_trade) == ("T1", "T2")


@settings(max_examples=25, deadline=None)
@given(st.randoms(use_true_random=False))
def test_linking_is_order_independent(rnd: random.Random) -> None:
    from asas.core.config import default_config_path, load_config

    cfg = load_config(default_config_path())
    events = [
        ev("T1", 1, "NEW", 0),
        ev("T1", 2, "CANCEL", 2),
        ev("T2", 1, "NEW", 3, original="T1"),
        ev("T3", 1, "NEW", 1, urn="U-1"),
        ev("T4", 1, "NEW", 2, urn="U-1"),
        ev("T5", 1, "NEW", 4),
    ]
    alerts = [alert("A1", "T1", "R200", 2), alert("A2", "T5", "R400", 4)]
    base = build_episodes(events, alerts, cfg).episodes
    rnd.shuffle(events)
    rnd.shuffle(alerts)
    assert build_episodes(events, alerts, cfg).episodes == base


# ---------------------------------------------------------------- signals / scoring / PIT


def test_mid_rank_percentile_does_not_flag_ties() -> None:
    values = tuple(sorted([Decimal("0.03")] * 99 + [Decimal("7")]))
    assert _percentile(values, Decimal("0.03")) == Decimal("49.50")
    assert _percentile(values, Decimal("7")) == Decimal("99.50")


def test_snapshot_is_point_in_time_and_reproducible(cfg: Config, dataset: SyntheticDataset) -> None:
    as_of = dataset.end - timedelta(days=40)
    visible = SourceBundle(
        trade_events=tuple(e for e in dataset.bundle.trade_events if e.record_time <= as_of),
        alerts=tuple(a for a in dataset.bundle.alerts if a.record_time <= as_of),
        outcomes=tuple(o for o in dataset.bundle.outcomes if o.decided_at < as_of),
    )
    future = SourceBundle(
        trade_events=dataset.bundle.trade_events,
        alerts=dataset.bundle.alerts,
        outcomes=dataset.bundle.outcomes,
    )
    snap_a = build_snapshot(visible, cfg, policy(cfg), as_of)
    snap_b = build_snapshot(visible, cfg, policy(cfg), as_of)
    assert snap_a.snapshot_id == snap_b.snapshot_id
    assert {k: v.total for k, v in snap_a.scores.items()} == {
        k: v.total for k, v in snap_b.scores.items()
    }
    leaky = build_snapshot(future, cfg, policy(cfg), as_of)
    # signals are computed only from records at or before eval time: no future leakage
    for eid, sig in snap_a.signal_set.signals.items():
        if (
            eid in leaky.signal_set.signals
            and snap_a.episodes_by_id[eid].trade_ids == leaky.episodes_by_id[eid].trade_ids
        ):
            assert sig.numeric.get("n_events") == leaky.signal_set.signals[eid].numeric.get(
                "n_events"
            )


def test_score_is_a_sum_of_named_components(cfg: Config, dataset: SyntheticDataset) -> None:
    snap = build_snapshot(dataset.bundle, cfg, policy(cfg), dataset.end)
    for score in list(snap.scores.values())[:200]:
        assert sum(c.contribution for c in score.components) == score.total
        assert {c.name for c in score.components} == {
            "detections",
            "outliers",
            "rarity",
            "materiality",
            "data_quality",
        }


# ---------------------------------------------------------------- rules DSL


def test_rule_dsl_validation(cfg: Config) -> None:
    bad = RuleSpec(
        rule_id="DRX",
        subrule_id="DRX.1",
        description="d",
        severity="EXTREME",
        conditions=(
            Condition(signal="made_up", op=Op.GE, value="1"),
            Condition(signal="has_cancel", op=Op.GE, value="true"),
            Condition(signal="notional_usd_max", op=Op.GE, param="missing"),
        ),
        parameters={},
    )
    problems = validate_rule(bad, cfg)
    assert any("unknown signal" in p for p in problems)
    assert any("flags support" in p for p in problems)
    assert any("not defined" in p for p in problems)
    assert any("severity" in p for p in problems)


def test_rule_evaluation_names_matched_conditions(cfg: Config, dataset: SyntheticDataset) -> None:
    snap = build_snapshot(dataset.bundle, cfg, policy(cfg), dataset.end)
    rule = RuleSpec(
        rule_id="DRT",
        subrule_id="DRT.1",
        description="d",
        severity="LOW",
        conditions=(Condition(signal="has_cancel", op=Op.EQ, value="true"),),
        parameters={},
    )
    fired = [evaluate_rule(rule, s) for s in snap.signal_set.signals.values()]
    hits = [d for d in fired if d is not None]
    assert hits and hits[0].matched == ("has_cancel eq true",)


# ---------------------------------------------------------------- hypotheses


def _episodes_by_scenario(snap, truth, scenario: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        e.episode_id
        for e in snap.episodes
        if truth.scenario_by_trade.get(e.trade_ids[0]) == scenario and e.alert_ids
    ]


@pytest.mark.parametrize(
    ("scenario", "expected", "klass"),
    [
        ("FAT_FINGER", "PRICE_CORRECTION", HypothesisClass.BENIGN),
        ("CANCEL_REBOOK", "CANCEL_REBOOK_CORRECTION", HypothesisClass.BENIGN),
        ("OFF_MARKET", "OFF_MARKET_AMENDMENT", HypothesisClass.ANOMALOUS),
        ("REBOOK_REPRICE", "REBOOK_ECONOMICS_CHANGED", HypothesisClass.ANOMALOUS),
        ("WINDOW_DRESSING", "PERIOD_END_ROUND_TRIP", HypothesisClass.ANOMALOUS),
    ],
)
def test_deterministic_assessment_by_scenario(
    cfg: Config, dataset: SyntheticDataset, scenario: str, expected: str, klass: HypothesisClass
) -> None:
    snap = build_snapshot(dataset.bundle, cfg, policy(cfg), dataset.end)
    late = [
        e
        for e in _episodes_by_scenario(snap, dataset.truth, scenario)
        if snap.episodes_by_id[e].start > dataset.end - timedelta(days=45)
    ]
    assert late
    conclusions = Counter(assess(snap, e, SYSTEM).adjudication.conclusion for e in late)
    assert conclusions.most_common(1)[0][0] == expected
    assert assess(snap, late[0], SYSTEM).adjudication.klass in (klass, None)


def test_late_booking_is_never_confirmed_from_absence_of_contradiction(
    cfg: Config, dataset: SyntheticDataset
) -> None:
    snap = build_snapshot(dataset.bundle, cfg, policy(cfg), dataset.end)
    for eid in _episodes_by_scenario(snap, dataset.truth, "LATE_BOOKING")[:10]:
        adjudication = assess(snap, eid, SYSTEM).adjudication
        assert (
            adjudication.conclusion is None
            and adjudication.abstain_reason == "INSUFFICIENT_EVIDENCE"
        )
        assert "SYSTEM_INCIDENT_REFERENCE" in adjudication.missing


def test_unused_ruleset_models_are_valid(cfg: Config) -> None:
    assert RuleSet(rules=load_ruleset(ruleset_path(), cfg).rules).rules
