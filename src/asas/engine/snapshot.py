"""A frozen, versioned view of everything known at `as_of`.

Built by a pure function from a point-in-time source bundle, the active policy bundle and
human-confirmed links. Tools, the challenger, discovery and replay all read the snapshot;
nothing in it can be mutated.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from asas.core.config import Config
from asas.core.ids import content_hash
from asas.data.ingest import SourceBundle
from asas.domain.models import (
    Alert,
    Detection,
    Episode,
    EpisodeScore,
    LabelQuality,
    LinkProposal,
    PolicyBundle,
    ReviewOutcome,
    RfiAction,
    TradeEvent,
    TradeLink,
    TradePerson,
)
from asas.engine.linking import LinkingResult, build_episodes
from asas.engine.rules import evaluate_ruleset
from asas.engine.scoring import score_episode
from asas.engine.signals import SignalSet, compute_signals


@dataclass(frozen=True)
class Snapshot:
    as_of: datetime
    snapshot_id: str
    cfg: Config
    source: SourceBundle
    policy: PolicyBundle
    linking: LinkingResult
    signal_set: SignalSet
    scores: Mapping[str, EpisodeScore]
    detections: Mapping[str, tuple[Detection, ...]]
    events_by_trade: Mapping[str, tuple[TradeEvent, ...]]
    alerts_by_id: Mapping[str, Alert]
    alerts_by_trade: Mapping[str, tuple[Alert, ...]]
    outcomes_by_alert: Mapping[str, tuple[ReviewOutcome, ...]]
    rfi_open: frozenset[str]
    persons_by_trade: Mapping[str, TradePerson]
    episodes_by_id: Mapping[str, Episode]
    link_proposals: tuple[LinkProposal, ...]

    @property
    def episodes(self) -> tuple[Episode, ...]:
        return self.linking.episodes

    def episode_rules(self, episode_id: str) -> tuple[str, ...]:
        ep = self.episodes_by_id[episode_id]
        return tuple(sorted({self.alerts_by_id[a].rule_id for a in ep.alert_ids}))

    def curated_label(self, episode_id: str) -> str | None:
        """ESCALATED if any curated outcome escalated; CLEARED if all curated cleared."""
        labels = [
            o.label.value
            for a in self.episodes_by_id[episode_id].alert_ids
            for o in self.outcomes_by_alert.get(a, ())
            if o.quality is LabelQuality.CURATED
        ]
        if not labels:
            return None
        return "ESCALATED" if "ESCALATED" in labels else "CLEARED"


def _open_rfis(bundle: SourceBundle) -> frozenset[str]:
    latest: dict[str, RfiAction] = {}
    for r in sorted(bundle.rfi_events, key=lambda r: (r.record_time, r.action.value)):
        latest[r.alert_id] = r.action
    return frozenset(a for a, action in latest.items() if action is RfiAction.OPENED)


def build_snapshot(
    source: SourceBundle,
    cfg: Config,
    policy: PolicyBundle,
    as_of: datetime,
    confirmed_links: Sequence[TradeLink] = (),
    link_proposals: Sequence[LinkProposal] = (),
    eval_times: Mapping[str, datetime] | None = None,
) -> Snapshot:
    events_by_trade: dict[str, list[TradeEvent]] = defaultdict(list)
    for e in source.trade_events:
        events_by_trade[e.trade_id].append(e)
    alerts_by_trade: dict[str, list[Alert]] = defaultdict(list)
    for a in source.alerts:
        alerts_by_trade[a.trade_id].append(a)
    outcomes: dict[str, list[ReviewOutcome]] = defaultdict(list)
    for o in source.outcomes:
        outcomes[o.alert_id].append(o)

    linking = build_episodes(source.trade_events, source.alerts, cfg, confirmed_links)
    times = dict(eval_times) if eval_times else {}
    for ep in linking.episodes:
        times.setdefault(ep.episode_id, as_of)
    signal_set = compute_signals(linking.episodes, events_by_trade, alerts_by_trade, times, cfg)
    rfi_open = _open_rfis(source)
    scores: dict[str, EpisodeScore] = {}
    detections: dict[str, tuple[Detection, ...]] = {}
    alerts_by_id = {a.alert_id: a for a in source.alerts}
    for ep in linking.episodes:
        signals = signal_set.signals[ep.episode_id]
        rules = [alerts_by_id[a].rule_id for a in ep.alert_ids]
        scores[ep.episode_id] = score_episode(
            ep,
            signals,
            signal_set.baseline_for(signals.eval_time),
            rules,
            any(a in rfi_open for a in ep.alert_ids),
            cfg,
        )
        detections[ep.episode_id] = evaluate_ruleset(policy.ruleset.rules, signals)

    snapshot_id = content_hash(
        {
            "as_of": as_of,
            "cfg": cfg.fingerprint,
            "policy": policy.bundle_id,
            "linking": linking.version,
            "trades": len(source.trade_events),
            "alerts": len(source.alerts),
            "outcomes": len(source.outcomes),
            "links": [x.model_dump() for x in confirmed_links],
        }
    )[:16]
    return Snapshot(
        as_of=as_of,
        snapshot_id=snapshot_id,
        cfg=cfg,
        source=source,
        policy=policy,
        linking=linking,
        signal_set=signal_set,
        scores=scores,
        detections=detections,
        events_by_trade={
            t: tuple(sorted(v, key=lambda e: e.version)) for t, v in events_by_trade.items()
        },
        alerts_by_id=alerts_by_id,
        alerts_by_trade={t: tuple(v) for t, v in alerts_by_trade.items()},
        outcomes_by_alert={a: tuple(v) for a, v in outcomes.items()},
        rfi_open=rfi_open,
        persons_by_trade={p.trade_id: p for p in source.trade_persons},
        episodes_by_id={e.episode_id: e for e in linking.episodes},
        link_proposals=tuple(link_proposals),
    )
