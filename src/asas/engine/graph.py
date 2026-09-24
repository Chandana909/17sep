"""Evidence graph: alerts, trades, versions, rules, subrules, parameters, entities, episodes,
outcomes, patterns and proposals, joined by typed, provenance-carrying, versioned edges.

Deterministic edges are CANONICAL. Agent-proposed relationships appear as PROPOSED or
VERIFIED and never change canonical episodes until a human confirms them.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from asas.core.ids import content_hash
from asas.domain.models import LinkProposal, Pattern
from asas.engine.snapshot import Snapshot


class GraphNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node_id: str
    kind: str
    label: str
    props: dict[str, str] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    src: str
    dst: str
    kind: str
    status: str
    provenance: str
    evidence: dict[str, str] = Field(default_factory=dict)


class EvidenceGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    snapshot_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


class Neighborhood(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    root: str
    depth: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool


def build_graph(
    snap: Snapshot,
    proposals: Iterable[LinkProposal] = (),
    patterns: Iterable[Pattern] = (),
) -> EvidenceGraph:
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    det = f"deterministic:{snap.linking.version}"

    def node(node_id: str, kind: str, label: str, **props: str) -> str:
        nodes.setdefault(node_id, GraphNode(node_id=node_id, kind=kind, label=label, props=props))
        return node_id

    def edge(
        src: str,
        dst: str,
        kind: str,
        status: str = "CANONICAL",
        provenance: str = det,
        **evidence: str,
    ) -> None:
        edges.append(
            GraphEdge(
                src=src, dst=dst, kind=kind, status=status, provenance=provenance, evidence=evidence
            )
        )

    for rule in snap.policy.ruleset.rules:
        r = node(f"rule:{rule.rule_id}", "rule", rule.rule_id)
        s = node(f"subrule:{rule.subrule_id}", "subrule", rule.subrule_id, severity=rule.severity)
        edge(s, r, "SUBRULE_OF", provenance=f"policy:{snap.policy.bundle_id}")
        for name, value in sorted(rule.parameters.items()):
            p = node(f"param:{rule.subrule_id}:{name}", "parameter", name, value=value)
            edge(s, p, "USES_PARAMETER", provenance=f"policy:{snap.policy.bundle_id}")

    for trade_id, events in sorted(snap.events_by_trade.items()):
        first = events[0]
        t = node(f"trade:{trade_id}", "trade", trade_id, source=first.source)
        edge(t, node(f"book:{first.book}", "book", first.book), "BOOKED_IN")
        edge(
            node(f"book:{first.book}", "book", first.book),
            node(f"desk:{first.desk}", "desk", first.desk),
            "PART_OF",
        )
        edge(
            t,
            node(f"instrument:{first.instrument_id}", "instrument", first.instrument_id),
            "TRADES",
        )
        for e in events:
            v = node(
                f"version:{trade_id}:{e.version}",
                "trade_version",
                f"{trade_id} v{e.version}",
                event_type=e.event_type.value,
            )
            edge(v, t, "VERSION_OF")

    for ep in snap.episodes:
        epn = node(
            f"episode:{ep.episode_id}",
            "episode",
            ep.episode_id,
            signature=snap.signal_set.signals[ep.episode_id].labels["sequence_signature"],
        )
        for trade_id in ep.trade_ids:
            if trade_id in snap.events_by_trade:
                edge(f"trade:{trade_id}", epn, "MEMBER_OF")
        for link in ep.links:
            edge(
                f"trade:{link.src}",
                f"trade:{link.dst}",
                link.kind.value,
                link.status.value,
                link.provenance,
                **link.evidence,
            )
        for link in ep.rejected_links:
            edge(
                f"trade:{link.src}",
                f"trade:{link.dst}",
                link.kind.value,
                "REJECTED",
                link.provenance,
                reason=link.reason,
            )

    for alert in sorted(snap.alerts_by_id.values(), key=lambda a: a.alert_id):
        a = node(f"alert:{alert.alert_id}", "alert", alert.alert_id, rule=alert.rule_id)
        target = (
            f"version:{alert.trade_id}:{alert.trade_version}"
            if alert.trade_version is not None and alert.trade_id in snap.events_by_trade
            else f"trade:{alert.trade_id}"
        )
        if target in nodes:
            edge(a, target, "ALERT_ON")
        edge(a, node(f"subrule:{alert.subrule_id}", "subrule", alert.subrule_id), "FIRED_BY")
        for o in snap.outcomes_by_alert.get(alert.alert_id, ()):
            on = node(f"outcome:{o.outcome_id}", "outcome", o.label.value, quality=o.quality.value)
            edge(on, a, "OUTCOME_OF", provenance=f"human:{o.quality.value}")

    for proposal in proposals:
        if f"trade:{proposal.trade_a}" in nodes and f"trade:{proposal.trade_b}" in nodes:
            edge(
                f"trade:{proposal.trade_b}",
                f"trade:{proposal.trade_a}",
                "AGENT_LINK",
                proposal.status.value,
                f"agent:{proposal.proposed_by}",
                **proposal.verification,
            )
    for pattern in patterns:
        pn = node(
            f"pattern:{pattern.pattern_id}",
            "pattern",
            pattern.kind.value,
            items=";".join(pattern.items),
        )
        for eid in pattern.sample_episode_ids:
            if f"episode:{eid}" in nodes:
                edge(f"episode:{eid}", pn, "MATCHES_PATTERN", provenance="discovery")

    ordered_nodes = tuple(sorted(nodes.values(), key=lambda n: n.node_id))
    ordered_edges = tuple(sorted(edges, key=lambda e: (e.src, e.dst, e.kind)))
    version = content_hash([ordered_nodes, ordered_edges])[:16]
    return EvidenceGraph(
        version=version, snapshot_id=snap.snapshot_id, nodes=ordered_nodes, edges=ordered_edges
    )


def neighborhood(graph: EvidenceGraph, root: str, depth: int, max_nodes: int) -> Neighborhood:
    adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
    for e in graph.edges:
        adjacency[e.src].append(e)
        adjacency[e.dst].append(e)
    by_id = {n.node_id: n for n in graph.nodes}
    if root not in by_id:
        return Neighborhood(root=root, depth=depth, nodes=(), edges=(), truncated=False)
    seen = {root}
    kept: list[GraphEdge] = []
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    truncated = False
    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        for e in adjacency[current]:
            other = e.dst if e.src == current else e.src
            if other not in seen:
                if len(seen) >= max_nodes:
                    truncated = True
                    continue
                seen.add(other)
                queue.append((other, d + 1))
            kept.append(e)
    edges = tuple(
        sorted(
            {(e.src, e.dst, e.kind): e for e in kept if e.src in seen and e.dst in seen}.values(),
            key=lambda e: (e.src, e.dst, e.kind),
        )
    )
    return Neighborhood(
        root=root,
        depth=depth,
        nodes=tuple(by_id[n] for n in sorted(seen)),
        edges=edges,
        truncated=truncated,
    )
