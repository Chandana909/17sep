"""Deterministic detectors behind the Detection Challenger.

They surface candidate findings about existing rule behaviour: redundant detections on one
business event, alerts with a verified alternative explanation, alerts missing lifecycle
context, and blind spots (anomalies no production rule fired on). The challenger agent
chooses what to examine and gathers evidence; `verify_finding` decides what is confirmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from asas.core.ids import stable_id
from asas.domain.models import FindingKind, HypothesisClass, LinkKind
from asas.engine import evidence as ev
from asas.engine.hypotheses import Assessment, EvidenceBag, ToolRequest, need
from asas.engine.snapshot import Snapshot


@dataclass(frozen=True)
class FindingCandidate:
    finding_id: str
    kind: FindingKind
    episode_id: str
    rule_ids: tuple[str, ...]
    alert_ids: tuple[str, ...]
    facts: Mapping[str, str]
    verify_requests: tuple[ToolRequest, ...]


def candidate_findings(
    snap: Snapshot, assessments: Mapping[str, Assessment]
) -> list[FindingCandidate]:
    cfg = snap.cfg
    min_alerts = cfg.integer("challenger", "redundancy_min_alerts")
    exposure_rules = set(cfg.strings("challenger", "exposure_rules"))
    sampling_rules = set(cfg.strings("challenger", "sampling_rules"))
    out: list[FindingCandidate] = []
    for ep in snap.episodes:
        rules = snap.episode_rules(ep.episode_id)
        detection_rules = tuple(r for r in rules if r not in sampling_rules)
        assessment = assessments.get(ep.episode_id)
        eid = ep.episode_id
        if len(ep.alert_ids) >= min_alerts:
            out.append(
                FindingCandidate(
                    stable_id("FND", "REDUNDANT", eid),
                    FindingKind.REDUNDANT_DETECTION,
                    eid,
                    rules,
                    ep.alert_ids,
                    {
                        "alerts": str(len(ep.alert_ids)),
                        "trades": str(len(ep.trade_ids)),
                        "rules": ",".join(rules),
                    },
                    (ToolRequest.of("get_related_alerts", episode_id=eid),),
                )
            )
        if (
            assessment
            and detection_rules
            and assessment.adjudication.klass is HypothesisClass.BENIGN
        ):
            out.append(
                FindingCandidate(
                    stable_id("FND", "ALTERNATIVE", eid),
                    FindingKind.ALTERNATIVE_EXPLANATION,
                    eid,
                    detection_rules,
                    ep.alert_ids,
                    {
                        "verified_explanation": str(assessment.adjudication.conclusion),
                        "rules": ",".join(detection_rules),
                    },
                    (
                        ToolRequest.of("get_episode", episode_id=eid),
                        *(ToolRequest.of("get_rule", rule_id=r) for r in detection_rules),
                    ),
                )
            )
        rebook_of = {x.src: x.dst for x in ep.links if x.kind is LinkKind.REBOOK_OF}
        for alert_id in ep.alert_ids:
            alert = snap.alerts_by_id[alert_id]
            if alert.rule_id in exposure_rules and alert.trade_id in rebook_of:
                original = rebook_of[alert.trade_id]
                out.append(
                    FindingCandidate(
                        stable_id("FND", "CONTEXT", alert_id),
                        FindingKind.MISSING_CONTEXT,
                        eid,
                        (alert.rule_id,),
                        (alert_id,),
                        {
                            "alerted_trade": alert.trade_id,
                            "rebook_of": original,
                            "rule": alert.rule_id,
                        },
                        (
                            ToolRequest.of(
                                "compare_trades", trade_a=original, trade_b=alert.trade_id
                            ),
                        ),
                    )
                )
        if not detection_rules and assessment is not None:
            anomalous = assessment.adjudication.klass is HypothesisClass.ANOMALOUS
            if anomalous or snap.scores[eid].band == "HIGH":
                out.append(
                    FindingCandidate(
                        stable_id("FND", "BLIND", eid),
                        FindingKind.BLIND_SPOT,
                        eid,
                        rules,
                        ep.alert_ids,
                        {
                            "assessment": str(assessment.adjudication.conclusion),
                            "band": snap.scores[eid].band,
                            "sampled_only": str(bool(rules)).lower(),
                        },
                        (
                            ToolRequest.of("get_event_sequence", episode_id=eid),
                            *(
                                ToolRequest.of("compare_trade_versions", trade_id=t)
                                for t in ep.trade_ids
                                if t in snap.events_by_trade
                            ),
                        ),
                    )
                )
    return sorted(out, key=lambda f: (f.kind.value, f.finding_id))


def verify_finding(
    candidate: FindingCandidate,
    bag: EvidenceBag,
    assessments: Mapping[str, Assessment],
    snap: Snapshot,
) -> tuple[bool, str]:
    for req in candidate.verify_requests:
        if not bag.has(req):
            return False, f"evidence not gathered: {req.key}"
    if candidate.kind is FindingKind.REDUNDANT_DETECTION:
        rel = need(bag, candidate.verify_requests[0], ev.RelatedAlerts)
        ok = len(rel.in_episode) >= snap.cfg.integer("challenger", "redundancy_min_alerts")
        return ok, "several alerts describe one business event" if ok else "single alert"
    if candidate.kind is FindingKind.ALTERNATIVE_EXPLANATION:
        a = assessments.get(candidate.episode_id)
        ok = a is not None and a.adjudication.klass is HypothesisClass.BENIGN
        return (
            ok,
            "a benign explanation is deterministically verified"
            if ok
            else "no verified explanation",
        )
    if candidate.kind is FindingKind.MISSING_CONTEXT:
        cmp = need(bag, candidate.verify_requests[0], ev.TradeComparison)
        tol = snap.cfg.decimal("hypotheses", "rebook_price_tolerance_pct")
        ok = (
            cmp.same_instrument
            and cmp.same_side
            and cmp.quantity_diff_pct == Decimal(0)
            and cmp.price_diff_pct is not None
            and cmp.price_diff_pct <= tol
        )
        return ok, (
            "alerted exposure is a like-for-like rebook, not new exposure"
            if ok
            else "rebook changed economics; exposure alert stands"
        )
    a = assessments.get(candidate.episode_id)
    ok = a is not None and (
        a.adjudication.klass is HypothesisClass.ANOMALOUS
        or snap.scores[candidate.episode_id].band == "HIGH"
    )
    return ok, "anomaly with no production detection" if ok else "not anomalous on re-check"
