"""Hypothesis catalog, evidence requirements, deterministic evaluators and adjudication.

Agents choose which hypotheses to pursue and which evidence to fetch. Whether a hypothesis
is SUPPORTED, CONTRADICTED or INSUFFICIENT is decided here, from tool outputs only. Absence
of contradiction is never confirmation: a missing fact yields INSUFFICIENT.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel

from asas.core.config import Config
from asas.core.errors import ToolError
from asas.core.security import Principal
from asas.domain.models import HypothesisClass, HypothesisStatus
from asas.engine import evidence as ev
from asas.engine.snapshot import Snapshot

# ---------------------------------------------------------------- evidence requests


@dataclass(frozen=True, order=True)
class ToolRequest:
    tool: str
    args: tuple[tuple[str, str], ...]

    @staticmethod
    def of(tool: str, **args: str) -> ToolRequest:
        return ToolRequest(tool, tuple(sorted(args.items())))

    @property
    def key(self) -> str:
        return f"{self.tool}({', '.join(f'{k}={v}' for k, v in self.args)})"

    @property
    def arg_dict(self) -> dict[str, str]:
        return dict(self.args)


EvidenceFn = Callable[[Snapshot, Mapping[str, str], Principal], BaseModel]

EVIDENCE_FUNCTIONS: Mapping[str, EvidenceFn] = {
    "get_alert": lambda s, a, p: ev.alert_view(s, a["alert_id"]),
    "get_episode": lambda s, a, p: ev.episode_view(s, a["episode_id"]),
    "get_trade": lambda s, a, p: ev.trade_view(s, a["trade_id"]),
    "get_trade_history": lambda s, a, p: ev.trade_history(s, a["trade_id"]),
    "compare_trade_versions": lambda s, a, p: ev.compare_versions(s, a["trade_id"]),
    "get_event_sequence": lambda s, a, p: ev.event_sequence(s, a["episode_id"]),
    "get_related_alerts": lambda s, a, p: ev.related_alerts(s, a["episode_id"]),
    "get_related_episodes": lambda s, a, p: ev.related_episodes(s, a["episode_id"]),
    "get_trader_baseline": lambda s, a, p: ev.trader_baseline(s, a["episode_id"], p),
    "get_peer_comparison": lambda s, a, p: ev.peer_comparison(s, a["episode_id"], a["signal"]),
    "get_recurrence": lambda s, a, p: ev.recurrence(s, a["episode_id"]),
    "get_prior_outcomes": lambda s, a, p: ev.prior_outcomes(s, a["episode_id"]),
    "get_rule": lambda s, a, p: ev.rule_view(s, a["rule_id"]),
    "compare_trades": lambda s, a, p: ev.compare_trades(s, a["trade_a"], a["trade_b"]),
}


class EvidenceBag:
    """Tool outputs available to evaluators, keyed by request. Lead-only results never enter."""

    def __init__(self, items: Mapping[str, BaseModel] | None = None) -> None:
        self._items: dict[str, BaseModel] = dict(items or {})

    def add(self, request: ToolRequest, output: BaseModel) -> None:
        self._items[request.key] = output

    def get(self, request: ToolRequest) -> BaseModel | None:
        return self._items.get(request.key)

    def has(self, request: ToolRequest) -> bool:
        return request.key in self._items


V = TypeVar("V", bound=BaseModel)


def need(bag: EvidenceBag, request: ToolRequest, kind: type[V]) -> V:
    """Typed access to required evidence; evaluators only run once requirements are met."""
    output = bag.get(request)
    if not isinstance(output, kind):
        raise ToolError(f"required evidence missing: {request.key}")
    return output


# ---------------------------------------------------------------- catalog


@dataclass
class Evaluation:
    status: HypothesisStatus
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Subject:
    episode: ev.EpisodeView
    verified_links: tuple[tuple[str, str], ...]  # (cancelled trade, rebook trade)

    def signal(self, name: str) -> str | None:
        return self.episode.signals.get(name)

    def flag(self, name: str) -> bool:
        return self.episode.signals.get(name) == "true"

    def number(self, name: str) -> Decimal | None:
        raw = self.episode.signals.get(name)
        return Decimal(raw) if raw is not None else None

    def rebook_pairs(self) -> list[tuple[str, str]]:
        pairs = [
            (link.dst, link.src)
            for link in self.episode.links
            if link.kind in ("REBOOK_OF", "AGENT_LINK")
        ]
        members = set(self.episode.trade_ids)
        pairs += [p for p in self.verified_links if p[0] in members or p[1] in members]
        return sorted(set(pairs))


Requirements = Callable[[Subject, Config], list[ToolRequest]]
Evaluator = Callable[[Subject, EvidenceBag, Config], Evaluation]
Applies = Callable[[Subject, Config], bool]


@dataclass(frozen=True)
class HypothesisDef:
    type: str
    klass: HypothesisClass
    description: str
    applies: Applies
    requirements: Requirements
    evaluate: Evaluator


def _p(cfg: Config, name: str) -> Decimal:
    return cfg.decimal("hypotheses", name)


def _diffs(subject: Subject, bag: EvidenceBag) -> list[ev.VersionDiff]:
    out: list[ev.VersionDiff] = []
    for t in subject.episode.trade_ids:
        request = ToolRequest.of("compare_trade_versions", trade_id=t)
        out.extend(need(bag, request, ev.VersionComparison).diffs)
    return out


def _versions_needed(subject: Subject, _cfg: Config) -> list[ToolRequest]:
    return [ToolRequest.of("compare_trade_versions", trade_id=t) for t in subject.episode.trade_ids]


# ---- ROUTINE_EXECUTION


def _routine_applies(s: Subject, _cfg: Config) -> bool:
    return s.number("n_events") == Decimal(1) and s.number("n_trades") == Decimal(1)


def _routine_requires(s: Subject, _cfg: Config) -> list[ToolRequest]:
    return [ToolRequest.of("get_trade_history", trade_id=t) for t in s.episode.trade_ids]


def _routine_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    request = ToolRequest.of("get_trade_history", trade_id=s.episode.trade_ids[0])
    history = need(bag, request, ev.TradeHistory)
    e = Evaluation(HypothesisStatus.SUPPORTED)
    v = history.versions[0]
    latency = ev.hours_between(v.event_time, v.record_time)
    if len(history.versions) != 1 or v.event_type != "NEW":
        e.contradicting.append("trade has more than a single booking event")
    if latency > _p(cfg, "routine_max_latency_hours"):
        e.contradicting.append("booking latency exceeds the routine limit")
    unusual = set(s.episode.outlier_signals) - set(
        cfg.strings("hypotheses", "routine_allowed_outliers")
    )
    if unusual:
        e.contradicting.append(f"outlier signals present: {', '.join(sorted(unusual))}")
    if e.contradicting:
        e.status = HypothesisStatus.CONTRADICTED
    else:
        e.supporting.append("single booking, timely record, no unexplained outliers")
    return e


# ---- PRICE_CORRECTION


def _amend_applies(s: Subject, _cfg: Config) -> bool:
    return s.flag("has_amend")


def _price_correction_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    amends = [d for d in _diffs(s, bag) if d.event_type == "AMEND"]
    if not amends:
        return Evaluation(HypothesisStatus.INSUFFICIENT, missing=["amendment_versions"])
    e = Evaluation(HypothesisStatus.SUPPORTED)
    for d in amends:
        label = f"v{d.from_version}->v{d.to_version}"
        if d.price_change_pct is None:
            e.missing.append(f"price_on_{label}")
            continue
        if d.price_change_pct == 0:
            e.contradicting.append(f"{label} did not change the price")
        if d.price_change_pct > _p(cfg, "price_correction_max_pct"):
            e.contradicting.append(f"{label} price change exceeds correction tolerance")
        if d.quantity_changed or d.side_changed or d.instrument_changed:
            e.contradicting.append(f"{label} changed economics beyond price")
        if d.hours_after_booking > _p(cfg, "correction_window_hours"):
            e.contradicting.append(f"{label} came too long after booking to be a typo fix")
        if d.crosses_period_end:
            e.contradicting.append(f"{label} crossed a period end")
    if e.contradicting:
        e.status = HypothesisStatus.CONTRADICTED
    elif e.missing:
        e.status = HypothesisStatus.INSUFFICIENT
    else:
        e.supporting.append("every amendment is a prompt, bounded, price-only correction")
    return e


# ---- CANCEL_REBOOK_CORRECTION


def _cancel_applies(s: Subject, _cfg: Config) -> bool:
    return s.flag("has_cancel")


def _rebook_requires(s: Subject, _cfg: Config) -> list[ToolRequest]:
    reqs = [ToolRequest.of("get_event_sequence", episode_id=s.episode.episode_id)]
    reqs += [ToolRequest.of("compare_trades", trade_a=a, trade_b=b) for a, b in s.rebook_pairs()]
    return reqs


def _cancelled_trades(s: Subject, bag: EvidenceBag) -> list[str]:
    request = ToolRequest.of("get_event_sequence", episode_id=s.episode.episode_id)
    seq = need(bag, request, ev.EventSequence)
    return sorted({x.trade_id for x in seq.events if x.event_type == "CANCEL"})


def _rebook_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    e = Evaluation(HypothesisStatus.SUPPORTED)
    pairs = s.rebook_pairs()
    for cancelled in _cancelled_trades(s, bag):
        linked = [p for p in pairs if p[0] == cancelled]
        if not linked:
            e.missing.append(f"verified_rebook_link_for:{cancelled}")
            continue
        for a, b in linked:
            request = ToolRequest.of("compare_trades", trade_a=a, trade_b=b)
            cmp = need(bag, request, ev.TradeComparison)
            if not (cmp.same_instrument and cmp.same_side):
                e.contradicting.append(f"rebook {b} changes instrument or side")
            if cmp.quantity_diff_pct is None or cmp.quantity_diff_pct > _p(
                cfg, "rebook_qty_tolerance_pct"
            ):
                e.contradicting.append(f"rebook {b} changes quantity")
            if cmp.price_diff_pct is None or cmp.price_diff_pct > _p(
                cfg, "rebook_price_tolerance_pct"
            ):
                e.contradicting.append(f"rebook {b} changes price")
            if cmp.minutes_after_cancel is None or cmp.minutes_after_cancel > _p(
                cfg, "rebook_max_minutes"
            ):
                e.contradicting.append(f"rebook {b} is not prompt after the cancellation")
            if not e.contradicting:
                e.supporting.append(f"{b} re-books {a} with identical economics")
    if e.contradicting:
        e.status = HypothesisStatus.CONTRADICTED
    elif e.missing:
        e.status = HypothesisStatus.INSUFFICIENT
    return e


# ---- LATE_BOOKING_OPERATIONAL


def _late_applies(s: Subject, cfg: Config) -> bool:
    latency = s.number("booking_latency_hours_max")
    return latency is not None and latency >= _p(cfg, "late_booking_hours")


def _late_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    return Evaluation(
        HypothesisStatus.INSUFFICIENT,
        supporting=["booking latency confirmed from record and event times"],
        missing=list(cfg.strings("hypotheses", "late_booking_missing_evidence")),
    )


# ---- OFF_MARKET_AMENDMENT


def _off_market_requires(s: Subject, cfg: Config) -> list[ToolRequest]:
    return [
        *_versions_needed(s, cfg),
        ToolRequest.of(
            "get_peer_comparison", episode_id=s.episode.episode_id, signal="max_price_change_pct"
        ),
    ]


def _off_market_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    changes = [
        d.price_change_pct
        for d in _diffs(s, bag)
        if d.event_type == "AMEND" and d.price_change_pct is not None
    ]
    if not changes:
        return Evaluation(HypothesisStatus.CONTRADICTED, contradicting=["no priced amendment"])
    largest = max(changes)
    if largest <= _p(cfg, "off_market_min_pct"):
        return Evaluation(
            HypothesisStatus.CONTRADICTED,
            contradicting=["amendment size within normal correction range"],
        )
    peer = need(
        bag,
        ToolRequest.of(
            "get_peer_comparison", episode_id=s.episode.episode_id, signal="max_price_change_pct"
        ),
        ev.PeerComparison,
    )
    if not peer.available or peer.percentile is None:
        return Evaluation(HypothesisStatus.INSUFFICIENT, missing=["peer_baseline_for_price_change"])
    unusual = peer.percentile >= _p(cfg, "off_market_percentile") or (
        peer.robust_z is not None and peer.robust_z >= _p(cfg, "off_market_robust_z")
    )
    if not unusual:
        return Evaluation(
            HypothesisStatus.CONTRADICTED,
            contradicting=["amendment size is ordinary for the peer group"],
        )
    return Evaluation(
        HypothesisStatus.SUPPORTED,
        supporting=[f"amendment is an outlier versus {peer.peer_level} (n={peer.peer_n})"],
    )


# ---- PERIOD_END_ROUND_TRIP


def _round_trip_applies(s: Subject, _cfg: Config) -> bool:
    return s.flag("has_amend") and (s.flag("near_period_end") or s.flag("crosses_period_end"))


def _round_trip_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    for t in s.episode.trade_ids:
        cmp = need(bag, ToolRequest.of("compare_trade_versions", trade_id=t), ev.VersionComparison)
        bumps = [
            d
            for d in cmp.diffs
            if d.event_type == "AMEND"
            and d.price_change_pct is not None
            and d.price_change_pct >= _p(cfg, "round_trip_min_pct")
            and d.hours_to_period_end <= _p(cfg, "period_end_hours")
        ]
        reverts = [d for d in cmp.diffs if d.crosses_period_end and d.restores_original_price]
        if bumps and reverts and reverts[0].to_version > bumps[0].to_version:
            return Evaluation(
                HypothesisStatus.SUPPORTED,
                supporting=[f"{t}: price moved just before period end and restored after it"],
            )
    return Evaluation(
        HypothesisStatus.CONTRADICTED,
        contradicting=["no price move restored across the period end"],
    )


# ---- REBOOK_ECONOMICS_CHANGED


def _reprice_requires(s: Subject, cfg: Config) -> list[ToolRequest]:
    return [
        *_rebook_requires(s, cfg),
        ToolRequest.of("get_recurrence", episode_id=s.episode.episode_id),
    ]


def _reprice_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    pairs = s.rebook_pairs()
    if not pairs:
        return Evaluation(HypothesisStatus.INSUFFICIENT, missing=["verified_rebook_link"])
    rec = need(
        bag, ToolRequest.of("get_recurrence", episode_id=s.episode.episode_id), ev.Recurrence
    )
    for a, b in pairs:
        cmp = need(bag, ToolRequest.of("compare_trades", trade_a=a, trade_b=b), ev.TradeComparison)
        if cmp.price_diff_pct is not None and cmp.price_diff_pct > _p(
            cfg, "rebook_price_tolerance_pct"
        ):
            support = [f"rebook {b} changed the price of {a}"]
            if rec.book_rebook_episodes >= cfg.integer("hypotheses", "churn_min_rebook_episodes"):
                support.append("repeated cancel/rebook activity in the same book")
            return Evaluation(HypothesisStatus.SUPPORTED, supporting=support)
    return Evaluation(HypothesisStatus.CONTRADICTED, contradicting=["rebooks preserve economics"])


# ---- RECURRING_BENIGN_CONTEXT


def _always(_s: Subject, _cfg: Config) -> bool:
    return True


def _context_requires(s: Subject, _cfg: Config) -> list[ToolRequest]:
    return [ToolRequest.of("get_prior_outcomes", episode_id=s.episode.episode_id)]


def _context_eval(s: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    prior = need(
        bag, ToolRequest.of("get_prior_outcomes", episode_id=s.episode.episode_id), ev.PriorOutcomes
    )
    if prior.curated_escalated > 0:
        return Evaluation(
            HypothesisStatus.CONTRADICTED,
            contradicting=["curated history contains escalations for this pattern"],
        )
    if prior.curated_cleared >= cfg.integer("hypotheses", "context_min_cleared"):
        return Evaluation(
            HypothesisStatus.SUPPORTED,
            supporting=["curated history for this pattern is consistently cleared"],
        )
    return Evaluation(HypothesisStatus.INSUFFICIENT, missing=["curated_history_for_pattern"])


CATALOG: tuple[HypothesisDef, ...] = (
    HypothesisDef(
        "PERIOD_END_ROUND_TRIP",
        HypothesisClass.ANOMALOUS,
        "price moved into a period end and restored after it",
        _round_trip_applies,
        _versions_needed,
        _round_trip_eval,
    ),
    HypothesisDef(
        "OFF_MARKET_AMENDMENT",
        HypothesisClass.ANOMALOUS,
        "amendment moves price far outside the peer range",
        _amend_applies,
        _off_market_requires,
        _off_market_eval,
    ),
    HypothesisDef(
        "REBOOK_ECONOMICS_CHANGED",
        HypothesisClass.ANOMALOUS,
        "cancel/rebook presented as correction but economics changed",
        _cancel_applies,
        _reprice_requires,
        _reprice_eval,
    ),
    HypothesisDef(
        "CANCEL_REBOOK_CORRECTION",
        HypothesisClass.BENIGN,
        "cancelled and re-booked with identical economics",
        _cancel_applies,
        _rebook_requires,
        _rebook_eval,
    ),
    HypothesisDef(
        "PRICE_CORRECTION",
        HypothesisClass.BENIGN,
        "prompt, bounded, price-only correction of a booking error",
        _amend_applies,
        _versions_needed,
        _price_correction_eval,
    ),
    HypothesisDef(
        "LATE_BOOKING_OPERATIONAL",
        HypothesisClass.BENIGN,
        "booked late for an operational reason",
        _late_applies,
        _routine_requires,
        _late_eval,
    ),
    HypothesisDef(
        "ROUTINE_EXECUTION",
        HypothesisClass.BENIGN,
        "single timely booking with nothing unusual",
        _routine_applies,
        _routine_requires,
        _routine_eval,
    ),
    HypothesisDef(
        "RECURRING_BENIGN_CONTEXT",
        HypothesisClass.CONTEXT,
        "curated history of the same pattern (raises attention only)",
        _always,
        _context_requires,
        _context_eval,
    ),
)
CATALOG_BY_TYPE: Mapping[str, HypothesisDef] = {h.type: h for h in CATALOG}
CATALOG_VERSION = "hypotheses-1"


def applicable(subject: Subject, cfg: Config) -> list[HypothesisDef]:
    return [h for h in CATALOG if h.applies(subject, cfg)]


def missing_requests(
    h: HypothesisDef, subject: Subject, bag: EvidenceBag, cfg: Config
) -> list[ToolRequest]:
    return [r for r in h.requirements(subject, cfg) if not bag.has(r)]


def evaluate(h: HypothesisDef, subject: Subject, bag: EvidenceBag, cfg: Config) -> Evaluation:
    if not h.applies(subject, cfg):
        return Evaluation(
            HypothesisStatus.CONTRADICTED, contradicting=[f"preconditions not met: {h.description}"]
        )
    missing = missing_requests(h, subject, bag, cfg)
    if missing:
        return Evaluation(HypothesisStatus.INSUFFICIENT, missing=[r.key for r in missing])
    return h.evaluate(subject, bag, cfg)


@dataclass(frozen=True)
class Adjudication:
    conclusion: str | None
    klass: HypothesisClass | None
    abstain_reason: str | None
    missing: tuple[str, ...]
    contradicted: tuple[str, ...]
    history_adverse: bool


def adjudicate(results: Sequence[tuple[HypothesisDef, Evaluation]]) -> Adjudication:
    order = {h.type: i for i, h in enumerate(CATALOG)}
    ranked = sorted(results, key=lambda r: order[r[0].type])
    supported = [h for h, e in ranked if e.status is HypothesisStatus.SUPPORTED]
    contradicted = tuple(
        h.type
        for h, e in ranked
        if e.status is HypothesisStatus.CONTRADICTED and h.klass is HypothesisClass.BENIGN
    )
    history_adverse = any(
        h.klass is HypothesisClass.CONTEXT and e.status is HypothesisStatus.CONTRADICTED
        for h, e in ranked
    )
    anomalous = [h for h in supported if h.klass is HypothesisClass.ANOMALOUS]
    if anomalous:
        return Adjudication(
            anomalous[0].type, HypothesisClass.ANOMALOUS, None, (), contradicted, history_adverse
        )
    insufficient = [
        (h, e)
        for h, e in ranked
        if e.status is HypothesisStatus.INSUFFICIENT and h.klass is not HypothesisClass.CONTEXT
    ]
    missing = tuple(sorted({m for _, e in insufficient for m in e.missing}))
    benign = [h for h in supported if h.klass is HypothesisClass.BENIGN]
    if benign and not insufficient:
        return Adjudication(
            benign[0].type, HypothesisClass.BENIGN, None, (), contradicted, history_adverse
        )
    if insufficient:
        return Adjudication(
            None, None, "INSUFFICIENT_EVIDENCE", missing, contradicted, history_adverse
        )
    return Adjudication(None, None, "NO_SUPPORTED_EXPLANATION", (), contradicted, history_adverse)


@dataclass(frozen=True)
class Assessment:
    episode_id: str
    results: tuple[tuple[str, HypothesisStatus], ...]
    adjudication: Adjudication


def run_request(snap: Snapshot, request: ToolRequest, principal: Principal) -> BaseModel:
    return EVIDENCE_FUNCTIONS[request.tool](snap, request.arg_dict, principal)


def assess(snap: Snapshot, episode_id: str, principal: Principal) -> Assessment:
    """Exhaustive deterministic assessment: every applicable hypothesis with all its evidence.
    Used by the playbook fallback, the challenger's blind-spot scan, discovery and replay."""
    bag = EvidenceBag()
    episode = ev.episode_view(snap, episode_id)
    pairs = tuple(sorted(ev.verified_link_ids(snap)))
    subject = Subject(episode, pairs)
    results: list[tuple[HypothesisDef, Evaluation]] = []
    for h in applicable(subject, snap.cfg):
        for req in missing_requests(h, subject, bag, snap.cfg):
            bag.add(req, run_request(snap, req, principal))
        results.append((h, evaluate(h, subject, bag, snap.cfg)))
    return Assessment(
        episode_id, tuple((h.type, e.status) for h, e in results), adjudicate(results)
    )
