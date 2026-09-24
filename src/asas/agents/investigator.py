"""Investigation Agent.

The agent forms competing hypotheses, chooses which evidence to retrieve, asks the verifier to
evaluate hypotheses, proposes relationships deterministic linking missed, and concludes or
abstains. Conclusions are accepted only if the deterministic adjudicator agrees *and* every
applicable competing hypothesis was evaluated. The deterministic playbook implements the same
action interface and is the fallback whenever the model is disabled, fails or misbehaves.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from asas.agents.prompts import INVESTIGATOR, PromptTemplate
from asas.agents.runtime import ActionRejected, Counters
from asas.agents.tools import ToolContext, ToolExecutor, tool_catalog
from asas.agents.validation import validate_prose
from asas.core.ids import stable_id
from asas.domain.models import (
    EvidenceRecord,
    Hypothesis,
    HypothesisClass,
    HypothesisStatus,
    InvestigationResult,
    LinkProposal,
    LinkStatus,
)
from asas.engine import evidence as ev
from asas.engine import hypotheses as hyp
from asas.engine.hypotheses import CATALOG, CATALOG_BY_TYPE, EvidenceBag, Subject, ToolRequest

INVESTIGATOR_TOOLS = frozenset(
    {
        "get_alert",
        "get_episode",
        "get_trade",
        "get_trade_history",
        "compare_trade_versions",
        "get_event_sequence",
        "get_related_alerts",
        "get_related_episodes",
        "get_trader_baseline",
        "get_peer_comparison",
        "get_recurrence",
        "get_prior_outcomes",
        "get_rule",
        "get_subrules",
        "get_rule_parameters",
        "compare_trades",
        "search_evidence_graph",
        "find_similar_cases",
    }
)


# ---------------------------------------------------------------- actions


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HypothesisProposal(_Action):
    type: str
    rationale: str = ""


class ProposeHypotheses(_Action):
    action: Literal["propose_hypotheses"]
    hypotheses: list[HypothesisProposal]


class CallTool(_Action):
    action: Literal["call_tool"]
    tool: str
    args: dict[str, str]
    purpose: str = ""


class Evaluate(_Action):
    action: Literal["evaluate"]
    hypothesis_id: str


class ProposeLink(_Action):
    action: Literal["propose_link"]
    trade_a: str
    trade_b: str


class Conclude(_Action):
    action: Literal["conclude"]
    hypothesis_id: str
    summary: str = ""


class Abstain(_Action):
    action: Literal["abstain"]
    reason: str
    missing: list[str] = []


InvestigatorAction = Annotated[
    ProposeHypotheses | CallTool | Evaluate | ProposeLink | Conclude | Abstain,
    Field(discriminator="action"),
]
_ADAPTER: TypeAdapter[InvestigatorAction] = TypeAdapter(InvestigatorAction)


# ---------------------------------------------------------------- state


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    call_id: str
    tool: str
    args: dict[str, str]
    output_type: str
    output: dict[str, Any]
    facts: dict[str, str]
    lead_only: bool


class HypState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str
    type: str
    rationale: str
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting: list[str] = []
    contradicting: list[str] = []
    missing: list[str] = []
    evidence_ids: list[str] = []
    evaluated_at: int = -1
    needed: list[str] = []


class InvState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    episode_id: str
    hypotheses: list[HypState] = []
    evidence: list[EvidenceItem] = []
    proposals: list[LinkProposal] = []
    conclusion: str | None = None
    summary: str = ""
    abstained: bool = False
    abstain_reason: str | None = None
    missing: list[str] = []
    rejections: list[str] = []
    last_observation: dict[str, Any] = {}


# ---------------------------------------------------------------- helpers


def _bag(state: InvState) -> EvidenceBag:
    bag = EvidenceBag()
    for item in state.evidence:
        if item.lead_only or item.output_type not in ev.VIEW_TYPES:
            continue
        model = ev.VIEW_TYPES[item.output_type].model_validate(item.output)
        bag.add(ToolRequest.of(item.tool, **item.args), model)
    return bag


def _subject(state: InvState, ctx: ToolContext) -> Subject | None:
    request = ToolRequest.of("get_episode", episode_id=state.episode_id)
    episode = _bag(state).get(request)
    if not isinstance(episode, ev.EpisodeView):
        return None
    verified = set(ev.verified_link_ids(ctx.snapshot))
    verified |= {(p.trade_a, p.trade_b) for p in state.proposals if p.status is LinkStatus.VERIFIED}
    return Subject(episode, tuple(sorted(verified)))


def _progress(state: InvState) -> int:
    return len(state.evidence) + len(state.proposals)


def _evaluations(state: InvState) -> list[tuple[hyp.HypothesisDef, hyp.Evaluation]]:
    out = []
    for h in state.hypotheses:
        if h.evaluated_at >= 0:
            out.append(
                (
                    CATALOG_BY_TYPE[h.type],
                    hyp.Evaluation(
                        h.status, list(h.supporting), list(h.contradicting), list(h.missing)
                    ),
                )
            )
    return out


def _deterministic_summary(state: InvState, conclusion: str) -> str:
    h = next(x for x in state.hypotheses if x.type == conclusion)
    cites = " ".join(f"[{e}]" for e in h.evidence_ids[:6])
    return f"{conclusion} is supported by the verified evidence {cites}".strip()


# ---------------------------------------------------------------- program


class InvestigatorProgram:
    name = "investigator"
    version = "3.0"
    prompt: PromptTemplate = INVESTIGATOR
    tools = INVESTIGATOR_TOOLS
    state_type = InvState
    result_type = InvestigationResult

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id

    def start(self, subject: str, ctx: ToolContext) -> InvState:
        return InvState(case_id=self.case_id, episode_id=subject)

    def parse(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ADAPTER.validate_python(action).model_dump()
        except Exception as exc:  # pydantic ValidationError and friends
            raise ActionRejected(str(exc)) from exc

    # ------------------------------------------------------------ deterministic playbook

    def playbook(self, state: InvState, ctx: ToolContext) -> dict[str, Any]:
        cfg = ctx.snapshot.cfg
        subject = _subject(state, ctx)
        if subject is None:
            return {
                "action": "call_tool",
                "tool": "get_episode",
                "args": {"episode_id": state.episode_id},
                "purpose": "establish the episode",
            }
        if not state.hypotheses:
            return {
                "action": "propose_hypotheses",
                "hypotheses": [
                    {"type": h.type, "rationale": h.description}
                    for h in hyp.applicable(subject, cfg)
                ],
            }
        bag = _bag(state)
        link_action = self._link_step(state, subject, bag, ctx)
        if link_action is not None:
            return link_action
        for h in sorted(state.hypotheses, key=lambda x: [c.type for c in CATALOG].index(x.type)):
            definition = CATALOG_BY_TYPE[h.type]
            missing = hyp.missing_requests(definition, subject, bag, cfg)
            if missing:
                return {
                    "action": "call_tool",
                    "tool": missing[0].tool,
                    "args": missing[0].arg_dict,
                    "purpose": f"evidence for {h.type}",
                }
            needed = sorted(r.key for r in definition.requirements(subject, cfg))
            if h.status is HypothesisStatus.PROPOSED or needed != h.needed:
                return {"action": "evaluate", "hypothesis_id": h.hypothesis_id}
        adjudication = hyp.adjudicate(_evaluations(state))
        if adjudication.conclusion:
            target = next(x for x in state.hypotheses if x.type == adjudication.conclusion)
            return {
                "action": "conclude",
                "hypothesis_id": target.hypothesis_id,
                "summary": _deterministic_summary(state, adjudication.conclusion),
            }
        return {
            "action": "abstain",
            "reason": adjudication.abstain_reason or "NO_CONCLUSION",
            "missing": list(adjudication.missing),
        }

    def _link_step(
        self, state: InvState, subject: Subject, bag: EvidenceBag, ctx: ToolContext
    ) -> dict[str, Any] | None:
        needs_link = [
            m.split(":", 1)[1]
            for h in state.hypotheses
            for m in h.missing
            if m.startswith("verified_rebook_link_for:")
        ]
        if not needs_link:
            return None
        related_req = ToolRequest.of("get_related_episodes", episode_id=state.episode_id)
        related = bag.get(related_req)
        if not isinstance(related, ev.RelatedEpisodes):
            return {
                "action": "call_tool",
                "tool": related_req.tool,
                "args": related_req.arg_dict,
                "purpose": "look for unresolved rebook candidates",
            }
        proposed = {(p.trade_a, p.trade_b) for p in state.proposals}
        for pair in related.unresolved:
            if pair.cancelled_trade not in needs_link:
                continue
            key = (pair.cancelled_trade, pair.candidate_trade)
            if key in proposed:
                continue
            cmp_req = ToolRequest.of("compare_trades", trade_a=key[0], trade_b=key[1])
            if not bag.has(cmp_req):
                return {
                    "action": "call_tool",
                    "tool": "compare_trades",
                    "args": cmp_req.arg_dict,
                    "purpose": "test the candidate rebook economics",
                }
            return {"action": "propose_link", "trade_a": key[0], "trade_b": key[1]}
        return None

    # ------------------------------------------------------------ LLM observation

    def observe(self, state: InvState, ctx: ToolContext) -> dict[str, Any]:
        return {
            "task": "investigate",
            "episode_id": state.episode_id,
            "catalog": [
                {"type": h.type, "class": h.klass.value, "description": h.description}
                for h in CATALOG
            ],
            "tools": tool_catalog(self.tools),
            "hypotheses": [
                {
                    "id": h.hypothesis_id,
                    "type": h.type,
                    "status": h.status.value,
                    "missing": h.missing[:6],
                }
                for h in state.hypotheses
            ],
            "evidence": [
                {
                    "id": e.evidence_id,
                    "tool": e.tool,
                    "args": e.args,
                    "facts": dict(list(e.facts.items())[:60]),
                    "lead_only": e.lead_only,
                }
                for e in state.evidence[-12:]
            ],
            "link_proposals": [
                {"trade_a": p.trade_a, "trade_b": p.trade_b, "status": p.status.value}
                for p in state.proposals
            ],
            "rejections": state.rejections[-3:],
            "last_observation": state.last_observation,
            "action_schema": {
                "propose_hypotheses": {
                    "hypotheses": [{"type": "CATALOG_TYPE", "rationale": "text"}]
                },
                "call_tool": {"tool": "name", "args": {"arg": "value"}, "purpose": "text"},
                "evaluate": {"hypothesis_id": "H1"},
                "propose_link": {"trade_a": "cancelled trade", "trade_b": "rebook trade"},
                "conclude": {"hypothesis_id": "H1", "summary": "text citing [E1]"},
                "abstain": {"reason": "text", "missing": ["what is missing"]},
            },
        }

    # ------------------------------------------------------------ transition

    def apply(
        self,
        state: InvState,
        action: dict[str, Any],
        executor: ToolExecutor,
        step: int,
        ctx: ToolContext,
    ) -> tuple[InvState, dict[str, Any]]:
        parsed = _ADAPTER.validate_python(action)
        s = state.model_copy(deep=True)
        obs: dict[str, Any]
        if isinstance(parsed, ProposeHypotheses):
            obs = self._propose(s, parsed)
        elif isinstance(parsed, CallTool):
            obs = self._call(s, parsed, executor, step)
        elif isinstance(parsed, Evaluate):
            obs = self._evaluate(s, parsed, ctx)
        elif isinstance(parsed, ProposeLink):
            obs = self._link(s, parsed, ctx)
        elif isinstance(parsed, Conclude):
            obs = self._conclude(s, parsed, ctx)
        else:
            s.abstained = True
            s.abstain_reason = parsed.reason[:120]
            s.missing = sorted(set(parsed.missing) | {m for h in s.hypotheses for m in h.missing})
            obs = {"abstained": s.abstain_reason}
        s.last_observation = obs
        return s, obs

    def _propose(self, s: InvState, a: ProposeHypotheses) -> dict[str, Any]:
        added, rejected = [], []
        known = {h.type for h in s.hypotheses}
        for p in a.hypotheses:
            if p.type not in CATALOG_BY_TYPE:
                rejected.append(p.type)
                continue
            if p.type in known:
                continue
            rationale = (
                p.rationale
                if not validate_prose(p.rationale, set())
                else CATALOG_BY_TYPE[p.type].description
            )
            s.hypotheses.append(
                HypState(
                    hypothesis_id=f"H{len(s.hypotheses) + 1}", type=p.type, rationale=rationale
                )
            )
            known.add(p.type)
            added.append(p.type)
        if rejected:
            s.rejections.append(f"unknown hypothesis types: {', '.join(rejected)}")
        return {"added": added, "rejected": rejected}

    def _call(self, s: InvState, a: CallTool, executor: ToolExecutor, step: int) -> dict[str, Any]:
        result = executor.call(a.tool, a.args, step)
        if not result.ok or result.output is None or result.output_type is None:
            s.rejections.append(f"tool {a.tool} failed: {result.error}")
            return {"tool_call": 1, "error": result.error}
        evidence_id = f"E{len(s.evidence) + 1}"
        s.evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                call_id=result.call_id,
                tool=a.tool,
                args=result.args,
                output_type=result.output_type,
                output=result.output,
                facts=result.facts,
                lead_only=result.lead_only,
            )
        )
        return {"tool_call": 1, "evidence_id": evidence_id, "facts": result.facts}

    def _evaluate(self, s: InvState, a: Evaluate, ctx: ToolContext) -> dict[str, Any]:
        h = next((x for x in s.hypotheses if x.hypothesis_id == a.hypothesis_id), None)
        subject = _subject(s, ctx)
        if h is None or subject is None:
            s.rejections.append("evaluate needs a known hypothesis and the get_episode evidence")
            return {"error": "cannot evaluate yet"}
        definition = CATALOG_BY_TYPE[h.type]
        bag = _bag(s)
        result = hyp.evaluate(definition, subject, bag, ctx.snapshot.cfg)
        needed = {r.key for r in definition.requirements(subject, ctx.snapshot.cfg)}
        h.status = result.status
        h.supporting = result.supporting
        h.contradicting = result.contradicting
        h.missing = result.missing
        h.evidence_ids = [
            e.evidence_id for e in s.evidence if ToolRequest.of(e.tool, **e.args).key in needed
        ]
        h.evaluated_at = _progress(s)
        h.needed = sorted(needed)
        return {"hypothesis": h.type, "status": h.status.value, "missing": h.missing}

    def _link(self, s: InvState, a: ProposeLink, ctx: ToolContext) -> dict[str, Any]:
        cmp = _bag(s).get(ToolRequest.of("compare_trades", trade_a=a.trade_a, trade_b=a.trade_b))
        if not isinstance(cmp, ev.TradeComparison):
            s.rejections.append("propose_link requires compare_trades evidence for the pair")
            return {"error": "gather compare_trades first"}
        cfg = ctx.snapshot.cfg
        checks = {
            "a_cancelled": cmp.a_cancelled,
            "same_instrument": cmp.same_instrument,
            "same_book": cmp.same_book,
            "same_side": cmp.same_side,
            "quantity_match": cmp.quantity_diff_pct is not None
            and cmp.quantity_diff_pct <= cfg.decimal("hypotheses", "rebook_qty_tolerance_pct"),
            "price_match": cmp.price_diff_pct is not None
            and cmp.price_diff_pct <= cfg.decimal("hypotheses", "rebook_price_tolerance_pct"),
            "prompt_rebook": cmp.minutes_after_cancel is not None
            and cmp.minutes_after_cancel >= 0
            and cmp.minutes_after_cancel <= cfg.decimal("hypotheses", "rebook_max_minutes"),
            "no_existing_lineage": not cmp.b_has_original_link,
        }
        verified = all(checks.values())
        evidence_ids = [
            e.evidence_id
            for e in s.evidence
            if e.tool == "compare_trades" and e.args == {"trade_a": a.trade_a, "trade_b": a.trade_b}
        ]
        proposal = LinkProposal(
            proposal_id=stable_id("LNK", a.trade_a, a.trade_b),
            trade_a=a.trade_a,
            trade_b=a.trade_b,
            relation="REBOOK_OF",
            status=LinkStatus.VERIFIED if verified else LinkStatus.REJECTED,
            proposed_by=f"investigator:{s.case_id}",
            verification={k: str(v).lower() for k, v in checks.items()},
            evidence_ids=tuple(evidence_ids),
            created_at=ctx.snapshot.as_of,
        )
        s.proposals = [p for p in s.proposals if p.proposal_id != proposal.proposal_id] + [proposal]
        return {"link": proposal.proposal_id, "status": proposal.status.value}

    def _conclude(self, s: InvState, a: Conclude, ctx: ToolContext) -> dict[str, Any]:
        h = next((x for x in s.hypotheses if x.hypothesis_id == a.hypothesis_id), None)
        subject = _subject(s, ctx)
        if h is None or subject is None:
            s.rejections.append("conclude: unknown hypothesis")
            return {"rejected": "unknown hypothesis"}
        evaluated = {x.type for x in s.hypotheses if x.evaluated_at >= 0}
        pending = [
            d.type for d in hyp.applicable(subject, ctx.snapshot.cfg) if d.type not in evaluated
        ]
        if pending:
            s.rejections.append(f"conclude rejected: competing hypotheses not evaluated: {pending}")
            return {"rejected": "evaluate competing hypotheses first", "pending": pending}
        adjudication = hyp.adjudicate(_evaluations(s))
        if adjudication.conclusion != h.type:
            s.rejections.append(
                f"conclude rejected: verifier supports {adjudication.conclusion or 'no conclusion'}"
            )
            return {
                "rejected": "not supported by verification",
                "verifier": adjudication.conclusion,
            }
        errors = validate_prose(a.summary, {e.evidence_id for e in s.evidence if not e.lead_only})
        if errors:
            s.rejections.append(f"conclude rejected: summary invalid {errors}")
            return {"rejected": "summary invalid", "errors": errors}
        s.conclusion = h.type
        s.summary = a.summary
        return {"concluded": h.type}

    # ------------------------------------------------------------ lifecycle

    def done(self, state: InvState) -> bool:
        return state.conclusion is not None or state.abstained

    def exhaust(self, state: InvState, reason: str) -> InvState:
        s = state.model_copy(deep=True)
        s.abstained = True
        s.abstain_reason = reason
        s.missing = sorted({m for h in s.hypotheses for m in h.missing})
        return s

    def finalize(
        self, state: InvState, run_id: str, ctx: ToolContext, counters: Counters
    ) -> InvestigationResult:
        adjudication = hyp.adjudicate(_evaluations(state))
        klass = CATALOG_BY_TYPE[state.conclusion].klass if state.conclusion else None
        hypotheses = tuple(
            Hypothesis(
                hypothesis_id=h.hypothesis_id,
                type=h.type,
                klass=CATALOG_BY_TYPE[h.type].klass,
                status=h.status,
                rationale=h.rationale,
                supporting=tuple(h.supporting),
                contradicting=tuple(h.contradicting),
                missing=tuple(h.missing),
                evidence_ids=tuple(h.evidence_ids),
            )
            for h in state.hypotheses
        )
        evidence = tuple(
            EvidenceRecord(
                evidence_id=e.evidence_id,
                tool=e.tool,
                call_id=e.call_id,
                args=e.args,
                facts=e.facts,
                lead_only=e.lead_only,
            )
            for e in state.evidence
        )
        return InvestigationResult(
            run_id=run_id,
            case_id=state.case_id,
            episode_id=state.episode_id,
            policy="llm" if counters.model_calls else "playbook",
            fallback_used=counters.fallbacks > 0,
            hypotheses=hypotheses,
            evidence=evidence,
            conclusion=state.conclusion,
            conclusion_class=klass,
            abstained=state.conclusion is None,
            abstain_reason=None if state.conclusion else (state.abstain_reason or "NO_CONCLUSION"),
            missing_evidence=tuple(state.missing if state.conclusion is None else ()),
            contradictions=adjudication.contradicted,
            link_proposals=tuple(state.proposals),
            explanation=explain(state, klass),
            steps=counters.steps,
            tool_calls=counters.tool_calls,
        )


def explain(state: InvState, klass: HypothesisClass | None) -> str:
    """Deterministic, evidence-grounded narrative; agent summary is appended only if valid."""
    lines = [
        f"Episode {state.episode_id}: {len(state.hypotheses)} hypotheses considered, "
        f"{len(state.evidence)} evidence items retrieved."
    ]
    for h in state.hypotheses:
        facts = "; ".join(h.supporting or h.contradicting or h.missing) or "not evaluated"
        cites = " ".join(f"[{e}]" for e in h.evidence_ids)
        lines.append(f"- {h.type}: {h.status.value}. {facts} {cites}".rstrip())
    for p in state.proposals:
        lines.append(
            f"- Proposed relationship {p.trade_b} rebooks {p.trade_a}: {p.status.value} "
            "(quarantined until a human confirms)."
        )
    if state.conclusion:
        lines.append(
            f"Conclusion: {state.conclusion} ({klass.value if klass else ''}). {state.summary}"
        )
    else:
        missing = ", ".join(state.missing) or "none recorded"
        lines.append(f"Abstained: {state.abstain_reason}. Missing evidence: {missing}.")
    return "\n".join(lines)
