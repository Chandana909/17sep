"""Verified deviation analysis, data capabilities, residual adjudication and classification."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from asas.agents.tools import ToolContext, ToolExecutor
from asas.core.config import Config
from asas.core.security import SYSTEM
from asas.data.ingest import SourceBundle
from asas.data.synthetic import NOVEL_SCENARIOS, RISK_SCENARIOS, SyntheticDataset
from asas.domain.models import (
    Category,
    Episode,
    EpisodeSignals,
    HypothesisClass,
    HypothesisStatus,
    Recommendation,
)
from asas.engine import hypotheses as hyp
from asas.engine.capabilities import detect, rule_gaps
from asas.engine.classification import classify
from asas.engine.deviation import (
    ItemIndex,
    joint_rarity,
    peer_stat,
    residual,
    signal_deviation,
)
from asas.engine.signals import build_baseline, robust_scale
from asas.engine.snapshot import Snapshot, build_snapshot
from asas.services.demo import DemoResult
from test_engine import policy

T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def snap(cfg: Config, dataset: SyntheticDataset) -> Iterator[Snapshot]:
    yield build_snapshot(dataset.bundle, cfg, policy(cfg), dataset.end)


def _scenario(dataset: SyntheticDataset, snap: Snapshot, eid: str) -> str:
    return dataset.truth.scenario_by_trade.get(snap.episodes_by_id[eid].trade_ids[0], "?")


def _window(snap: Snapshot, days: int = 30) -> list[str]:
    start = snap.as_of - timedelta(days=days)
    return [e.episode_id for e in snap.episodes if e.end >= start]


# ---------------------------------------------------------------- robust statistics


def test_robust_scale_falls_back_to_mean_absolute_deviation(cfg: Config) -> None:
    spread = [Decimal(v) for v in (1, 2, 3, 4, 100)]
    assert robust_scale(spread, Decimal(3), cfg) == cfg.decimal("signals", "mad_scale")
    mostly_zero = [Decimal(0)] * 9 + [Decimal(10)]  # MAD is 0: a majority is identical
    expected = cfg.decimal("signals", "meanad_scale") * Decimal(1)
    assert robust_scale(mostly_zero, Decimal(0), cfg) == expected
    assert robust_scale([Decimal(5)] * 10, Decimal(5), cfg) == 0


def test_constant_population_departure_is_maximal_not_undefined(cfg: Config) -> None:
    population = tuple([Decimal(0)] * 40)
    above = peer_stat(population, Decimal("3"), "desk:EQ", "upper", cfg)
    assert above.robust_z == cfg.decimal("anomaly", "z_cap") and above.deviant
    same = peer_stat(population, Decimal(0), "desk:EQ", "upper", cfg)
    assert same.robust_z == 0 and not same.deviant


def test_screen_mode_all_requires_rank_and_distance(cfg: Config) -> None:
    # value 1 among 80 % zeros: high rank but only ~4 robust z; a lone 1 is not an outlier
    population = tuple(sorted([Decimal(0)] * 80 + [Decimal(1)] * 20))
    strict = peer_stat(population, Decimal(1), "desk:EQ", "upper", cfg)
    assert strict.percentile < cfg.decimal("anomaly", "screen_percentile")
    assert not strict.deviant
    loose_cfg = cfg.with_value("any", "anomaly", "screen_mode")
    assert peer_stat(population, Decimal(1), "desk:EQ", "upper", loose_cfg).deviant


# ---------------------------------------------------------------- verification


def _signals(eid: str, value: str | None, book: str = "B1") -> EpisodeSignals:
    numeric = {"n_events": Decimal(1)}
    if value is not None:
        numeric["max_qty_change_pct"] = Decimal(value)
    return EpisodeSignals(
        episode_id=eid,
        eval_time=T0,
        numeric=numeric,
        flags={},
        labels={
            "sequence_signature": "NEW>AMEND",
            "desk": "EQ",
            "book": book,
            "product_type": "EQUITY",
            "instrument_id": "I1",
            "desk_product": "EQ|EQUITY",
        },
    )


def _episode(eid: str, flags: tuple[str, ...] = ()) -> Episode:
    return Episode(
        episode_id=eid,
        trade_ids=(eid,),
        alert_ids=(),
        start=T0,
        end=T0,
        desks=("EQ",),
        books=("B1",),
        instrument_ids=("I1",),
        product_types=("EQUITY",),
        links=(),
        rejected_links=(),
        quality_flags=flags,
        linking_version="t",
    )


def _peers(n: int, value: str = "0") -> list[EpisodeSignals]:
    return [_signals(f"P{i}", value if i % 5 else "1") for i in range(n)]


def test_deviation_verified_only_when_confirmed_stable_and_clean(cfg: Config) -> None:
    baseline = build_baseline(T0, _peers(60), cfg)
    target = _signals("X", "400")
    ok = signal_deviation("max_qty_change_pct", target, _episode("X"), baseline, baseline, cfg)
    assert ok is not None and ok.verified and ok.stable is True
    assert ok.confirming_levels >= cfg.integer("anomaly", "min_confirming_levels")
    assert Decimal(0) < ok.strength <= Decimal(1)

    tainted = signal_deviation(
        "max_qty_change_pct", target, _episode("X", ("VERSION_GAP",)), baseline, baseline, cfg
    )
    assert tainted is not None and not tainted.verified
    assert "DATA_QUALITY:VERSION_GAP" in tainted.reasons

    shifted = build_baseline(T0, [_signals(f"S{i}", "500") for i in range(60)], cfg)
    unstable = signal_deviation("max_qty_change_pct", target, _episode("X"), baseline, shifted, cfg)
    assert unstable is not None and not unstable.verified
    assert "UNSTABLE_VS_SHIFTED_REFERENCE" in unstable.reasons

    small = build_baseline(T0, _peers(10), cfg)  # below signals.min_peer_n at every level
    thin = signal_deviation("max_qty_change_pct", target, _episode("X"), small, None, cfg)
    assert thin is not None and not thin.verified
    assert any(r.startswith("CONFIRMED_AT_0_OF_") for r in thin.reasons)


def test_ordinary_value_is_not_a_deviation(cfg: Config) -> None:
    baseline = build_baseline(T0, _peers(60), cfg)
    assert (
        signal_deviation(
            "max_qty_change_pct", _signals("X", "0"), _episode("X"), baseline, baseline, cfg
        )
        is None
    )


def test_joint_rarity_is_leave_one_out(cfg: Config) -> None:
    sig = _signals("X", "0")
    items = ("band:max_qty_change_pct:0", "sig=NEW>AMEND")
    counts = {(items[0],): 50, (items[1],): 50, items: 1}
    index = ItemIndex(reference_n=400, counts=counts)
    outside = joint_rarity(sig, index, cfg, in_reference=False)
    inside = joint_rarity(sig, index, cfg, in_reference=True)
    assert outside is not None and inside is not None
    assert outside.support == 1 and not outside.verified  # seen once elsewhere
    assert inside.support == 0 and inside.verified  # the only occurrence is itself


# ---------------------------------------------------------------- profiles on real data


def test_profiles_are_consistent_and_residual_never_exceeds_raw(snap: Snapshot) -> None:
    for eid in _window(snap):
        p = snap.deviations[eid]
        assert sum(c.contribution for c in p.components) == p.outlyingness
        for d in p.deviations:
            if d.verified:
                assert d.stable is not False and not any(
                    r.startswith("DATA_QUALITY") for r in d.reasons
                )
        assert residual(p, p.verified, snap.cfg)[1] == p.outlyingness
        assert residual(p, (), snap.cfg)[1] == 0


@settings(max_examples=60, deadline=None)
@given(st.data())
def test_residual_is_monotone_in_what_stays_unexplained(
    snap: Snapshot, data: st.DataObject
) -> None:
    candidates = [e for e in _window(snap, 60) if snap.deviations[e].verified]
    eid = data.draw(st.sampled_from(candidates))
    p = snap.deviations[eid]
    subset = data.draw(st.sets(st.sampled_from(p.verified)))
    smaller = data.draw(st.sets(st.sampled_from(sorted(subset)))) if subset else set()
    assert residual(p, smaller, snap.cfg)[1] <= residual(p, subset, snap.cfg)[1]
    assert residual(p, subset, snap.cfg)[1] <= p.outlyingness


def test_benign_scenarios_leave_no_unexplained_deviation(
    snap: Snapshot, dataset: SyntheticDataset
) -> None:
    found: Counter[str] = Counter()
    for eid in _window(snap, 45):
        scenario = _scenario(dataset, snap, eid)
        adjudication = hyp.assess(snap, eid, SYSTEM).adjudication
        if scenario not in RISK_SCENARIOS:
            assert not adjudication.unexplained, (scenario, adjudication)
        if scenario in NOVEL_SCENARIOS:
            assert adjudication.conclusion == "VERIFIED_PEER_DEVIATION", scenario
            assert adjudication.unexplained
            found[scenario] += 1
    assert set(found) == set(NOVEL_SCENARIOS)


def test_deviation_tool_names_populations_for_the_agent(snap: Snapshot) -> None:
    eid = next(e for e in _window(snap) if snap.deviations[e].verified)
    executor = ToolExecutor(
        ctx=ToolContext(snap, SYSTEM), allowed=frozenset({"get_deviation_profile"}), run_id="R"
    )
    result = executor.call("get_deviation_profile", {"episode_id": eid}, 0)
    assert result.ok and result.output_type == "DeviationView"
    summaries = [v for k, v in result.facts.items() if k.startswith("summaries.")]
    assert summaries and any("(n=" in s and "vs " in s for s in summaries)


# ---------------------------------------------------------------- adjudication


def _eval(status: HypothesisStatus, signals: tuple[str, ...] = ()) -> hyp.Evaluation:
    return hyp.Evaluation(status, signals=list(signals))


DEV = hyp.CATALOG_BY_TYPE["VERIFIED_PEER_DEVIATION"]
PRICE = hyp.CATALOG_BY_TYPE["PRICE_CORRECTION"]
OFF = hyp.CATALOG_BY_TYPE["OFF_MARKET_AMENDMENT"]
LATE = hyp.CATALOG_BY_TYPE["LATE_BOOKING_OPERATIONAL"]
S, C, INS = HypothesisStatus.SUPPORTED, HypothesisStatus.CONTRADICTED, HypothesisStatus.INSUFFICIENT


def test_adjudication_orders_typed_then_unexplained_then_benign(cfg: Config) -> None:
    explains = hyp.explains_map(cfg)
    typed = hyp.adjudicate(
        [(DEV, _eval(S, ("n_amends",))), (OFF, _eval(S)), (PRICE, _eval(C))], explains
    )
    assert typed.conclusion == "OFF_MARKET_AMENDMENT" and typed.unexplained == ("n_amends",)

    explained = hyp.adjudicate(
        [(DEV, _eval(S, ("max_price_change_pct",))), (PRICE, _eval(S)), (OFF, _eval(C))], explains
    )
    assert explained.conclusion == "PRICE_CORRECTION" and explained.klass is HypothesisClass.BENIGN
    assert explained.explained == ("max_price_change_pct",) and not explained.unexplained

    unexplained = hyp.adjudicate(
        [(DEV, _eval(S, ("max_qty_change_pct",))), (PRICE, _eval(S)), (OFF, _eval(C))], explains
    )
    assert unexplained.conclusion == "VERIFIED_PEER_DEVIATION"
    assert unexplained.unexplained == ("max_qty_change_pct",)

    pending = hyp.adjudicate(
        [(DEV, _eval(S, ("booking_latency_hours_max",))), (LATE, _eval(INS))], explains
    )
    assert pending.conclusion is None and pending.abstain_reason == "INSUFFICIENT_EVIDENCE"
    assert pending.pending == ("booking_latency_hours_max",)

    no_baseline = hyp.adjudicate([(DEV, _eval(INS)), (PRICE, _eval(S))], explains)
    assert no_baseline.conclusion is None  # the deviation check could not run: abstain


# ---------------------------------------------------------------- data capabilities


def _strip(bundle: SourceBundle, **fields: None) -> SourceBundle:
    return SourceBundle(
        trade_events=tuple(e.model_copy(update=fields) for e in bundle.trade_events),
        trade_persons=bundle.trade_persons,
        alerts=bundle.alerts,
        alert_annexes=bundle.alert_annexes,
        rfi_events=bundle.rfi_events,
        outcomes=bundle.outcomes,
    )


def test_missing_price_degrades_to_insufficient_never_contradicted(
    cfg: Config, dataset: SyntheticDataset
) -> None:
    bundle = _strip(dataset.bundle, price=None, notional_usd=None)
    snap = build_snapshot(bundle, cfg, policy(cfg), dataset.end)
    assert {"PRICE", "NOTIONAL_USD"} <= snap.capabilities.unavailable
    assert {"R100.1:PRICE", "R400.1:NOTIONAL_USD"} <= set(snap.rule_gaps)
    assert any(
        "FIELD_UNAVAILABLE:max_price_change_pct" in n
        for n in snap.deviations[snap.episodes[-1].episode_id].notes
    )
    for eid in _window(snap):
        a = hyp.assess(snap, eid, SYSTEM)
        statuses = dict(a.results)
        for name in ("OFF_MARKET_AMENDMENT", "PERIOD_END_ROUND_TRIP", "REBOOK_ECONOMICS_CHANGED"):
            assert statuses.get(name, INS) is INS, (name, statuses)
        # without price or notional nothing can be verified benign, so nothing can go bulk
        assert a.adjudication.klass is not HypothesisClass.BENIGN


def test_business_can_declare_a_field_unreliable(cfg: Config, dataset: SyntheticDataset) -> None:
    declared = cfg.with_value(["QUANTITY"], "data", "unavailable_fields")
    caps = detect(dataset.bundle, declared)
    assert caps.unavailable == frozenset({"QUANTITY"})
    assert caps.coverage["QUANTITY"] == Decimal(1)
    assert not caps.signal_available("max_qty_change_pct")
    assert rule_gaps(policy(cfg).ruleset.rules, caps) == ()


# ---------------------------------------------------------------- classification and decisions


def test_cases_are_classified_with_named_corroboration(demo: DemoResult) -> None:
    cases = demo.platform.cases()
    assert all(c.classification is not None for c in cases)
    by_rec: dict[Recommendation, set[Category]] = {}
    for c in cases:
        assert c.classification is not None
        by_rec.setdefault(c.recommendation, set()).add(c.classification.category)
        if c.recommendation is Recommendation.PROPOSED_BULK:
            assert c.classification.category is Category.VERIFIED_BENIGN
            assert not c.classification.unexplained  # rail: nothing unexplained goes bulk
            assert "NO_UNEXPLAINED_DEVIATION" in c.classification.corroboration
        if c.classification.category is Category.TYPED_ANOMALY:
            assert c.classification.corroboration[0].startswith("HYPOTHESIS_VERIFIED:")
            assert c.classification.severity in ("HIGH", "CRITICAL")
    assert Category.TYPED_ANOMALY in by_rec[Recommendation.ESCALATION_RECOMMENDED]


def test_severity_rises_with_materiality_and_confidence_counts_lines(demo: DemoResult) -> None:
    case = next(
        c
        for c in demo.platform.cases()
        if c.classification and c.classification.category is Category.TYPED_ANOMALY
    )
    assert case.investigation_run_id
    investigation = demo.platform.investigation(case.investigation_run_id)
    cfg = demo.platform.cfg
    small = classify(investigation, case.deviation, (), Decimal(1), cfg)
    large = classify(investigation, case.deviation, (), Decimal(10) ** 9, cfg)
    levels = cfg.strings("classification", "levels")
    assert levels.index(large.severity) == min(levels.index(small.severity) + 1, len(levels) - 1)
    lines = classify(investigation, case.deviation, ("R100", "R400"), Decimal(1), cfg)
    assert len(lines.corroboration) == len(small.corroboration) + 2
    assert levels  # confidence is derived from the number of named lines
    assert lines.confidence in ("MEDIUM", "HIGH")


def test_evaluation_shows_explained_away_outlyingness_is_sharper(demo: DemoResult) -> None:
    r = demo.evaluation.anomaly_ranking
    assert Decimal(r["unexplained_precision"]) == Decimal(1)
    assert Decimal(r["unexplained_precision"]) > Decimal(r["raw_outlying_precision"])
    assert Decimal(r["precision_at_k_verified_assessment"]) > Decimal(
        r["precision_at_k_attention_score"]
    )
    assert int(demo.evaluation.detection["risky_found_only_by_deviation_analysis"]) >= 3
    assert demo.evaluation.detection["recall_agentic_system"] == "1.0000"
