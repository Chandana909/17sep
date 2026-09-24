"""Detection Challenger agent: examines existing rule behaviour.

It works through candidate findings (redundant detections, verified alternative explanations,
missing lifecycle context, blind spots), gathers the evidence each needs, and confirms or
dismisses them. Confirmation requires deterministic verification; dismissal never removes a
finding - dismissed findings stay visible to humans.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from asas.agents.prompts import CHALLENGER, PromptTemplate
from asas.agents.runtime import ActionRejected, Counters
from asas.agents.tools import ToolContext, ToolExecutor, tool_catalog
from asas.agents.validation import validate_prose
from asas.domain.models import ChallengeFinding, FindingKind
from asas.engine import evidence as ev
from asas.engine.challenge import FindingCandidate, verify_finding
from asas.engine.hypotheses import Assessment, EvidenceBag, ToolRequest

CHALLENGER_TOOLS = frozenset(
    {
        "get_episode",
        "get_rule",
        "get_subrules",
        "get_rule_parameters",
        "get_related_alerts",
        "get_event_sequence",
        "compare_trade_versions",
        "compare_trades",
        "replay_rule",
        "search_evidence_graph",
    }
)
PRIORITY = {
    FindingKind.BLIND_SPOT: 0,
    FindingKind.MISSING_CONTEXT: 1,
    FindingKind.ALTERNATIVE_EXPLANATION: 2,
    FindingKind.REDUNDANT_DETECTION: 3,
}


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallTool(_A):
    action: Literal["call_tool"]
    tool: str
    args: dict[str, str]
    purpose: str = ""


class Confirm(_A):
    action: Literal["confirm"]
    finding_id: str
    note: str = ""


class Dismiss(_A):
    action: Literal["dismiss"]
    finding_id: str
    reason: str


class Finish(_A):
    action: Literal["finish"]


ChallengerAction = Annotated[CallTool | Confirm | Dismiss | Finish, Field(discriminator="action")]
_ADAPTER: TypeAdapter[ChallengerAction] = TypeAdapter(ChallengerAction)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    tool: str
    args: dict[str, str]
    output_type: str
    output: dict[str, Any]


class FindingState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str
    status: str = "PENDING"
    note: str = ""


class ChallengeState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[FindingState]
    evidence: list[Evidence] = []
    finished: bool = False
    rejections: list[str] = []


class ChallengeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str
    snapshot_id: str
    findings: tuple[ChallengeFinding, ...]


def _bag(state: ChallengeState) -> EvidenceBag:
    bag = EvidenceBag()
    for e in state.evidence:
        if e.output_type in ev.VIEW_TYPES:
            bag.add(
                ToolRequest.of(e.tool, **e.args),
                ev.VIEW_TYPES[e.output_type].model_validate(e.output),
            )
    return bag


class ChallengerProgram:
    name = "challenger"
    version = "2.0"
    prompt: PromptTemplate = CHALLENGER
    tools = CHALLENGER_TOOLS
    state_type = ChallengeState
    result_type = ChallengeReport

    def __init__(
        self, candidates: list[FindingCandidate], assessments: Mapping[str, Assessment]
    ) -> None:
        self.candidates = {c.finding_id: c for c in candidates}
        self.assessments = assessments

    def start(self, subject: str, ctx: ToolContext) -> ChallengeState:
        ordered = sorted(self.candidates.values(), key=lambda c: (PRIORITY[c.kind], c.finding_id))
        return ChallengeState(findings=[FindingState(finding_id=c.finding_id) for c in ordered])

    def parse(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ADAPTER.validate_python(action).model_dump()
        except Exception as exc:
            raise ActionRejected(str(exc)) from exc

    def playbook(self, state: ChallengeState, ctx: ToolContext) -> dict[str, Any]:
        bag = _bag(state)
        for f in state.findings:
            if f.status != "PENDING":
                continue
            candidate = self.candidates[f.finding_id]
            for req in candidate.verify_requests:
                if not bag.has(req):
                    return {
                        "action": "call_tool",
                        "tool": req.tool,
                        "args": req.arg_dict,
                        "purpose": f"verify {candidate.kind.value}",
                    }
            ok, why = verify_finding(candidate, bag, self.assessments, ctx.snapshot)
            if ok:
                return {"action": "confirm", "finding_id": f.finding_id, "note": why}
            return {"action": "dismiss", "finding_id": f.finding_id, "reason": why}
        return {"action": "finish"}

    def observe(self, state: ChallengeState, ctx: ToolContext) -> dict[str, Any]:
        pending = [f for f in state.findings if f.status == "PENDING"][:8]
        return {
            "task": "challenge existing detections",
            "tools": tool_catalog(self.tools),
            "pending": [
                {
                    "finding_id": f.finding_id,
                    "kind": self.candidates[f.finding_id].kind.value,
                    "episode_id": self.candidates[f.finding_id].episode_id,
                    "facts": dict(self.candidates[f.finding_id].facts),
                    "needs": [r.key for r in self.candidates[f.finding_id].verify_requests],
                }
                for f in pending
            ],
            "evidence": [
                {"id": e.evidence_id, "tool": e.tool, "args": e.args} for e in state.evidence[-10:]
            ],
            "rejections": state.rejections[-3:],
        }

    def apply(
        self,
        state: ChallengeState,
        action: dict[str, Any],
        executor: ToolExecutor,
        step: int,
        ctx: ToolContext,
    ) -> tuple[ChallengeState, dict[str, Any]]:
        parsed = _ADAPTER.validate_python(action)
        s = state.model_copy(deep=True)
        if isinstance(parsed, CallTool):
            result = executor.call(parsed.tool, parsed.args, step)
            if not result.ok or result.output is None or result.output_type is None:
                s.rejections.append(result.error)
                return s, {"tool_call": 1, "error": result.error}
            s.evidence.append(
                Evidence(
                    evidence_id=f"E{len(s.evidence) + 1}",
                    tool=parsed.tool,
                    args=result.args,
                    output_type=result.output_type,
                    output=result.output,
                )
            )
            return s, {"tool_call": 1, "facts": result.facts}
        if isinstance(parsed, Finish):
            s.finished = True
            return s, {"finished": True}
        target = next((f for f in s.findings if f.finding_id == parsed.finding_id), None)
        if target is None or target.status != "PENDING":
            s.rejections.append(f"unknown or closed finding {parsed.finding_id}")
            return s, {"rejected": "unknown finding"}
        if isinstance(parsed, Confirm):
            ok, why = verify_finding(
                self.candidates[target.finding_id], _bag(s), self.assessments, ctx.snapshot
            )
            if not ok:
                s.rejections.append(f"confirm rejected: {why}")
                return s, {"rejected": why}
            target.status, target.note = "CONFIRMED", why
            return s, {"confirmed": target.finding_id}
        target.status = "AGENT_DISMISSED"
        target.note = parsed.reason if not validate_prose(parsed.reason, set()) else "dismissed"
        return s, {"dismissed": target.finding_id}

    def done(self, state: ChallengeState) -> bool:
        return state.finished or all(f.status != "PENDING" for f in state.findings)

    def exhaust(self, state: ChallengeState, reason: str) -> ChallengeState:
        s = state.model_copy(deep=True)
        s.finished = True
        s.rejections.append(reason)
        return s

    def finalize(
        self, state: ChallengeState, run_id: str, ctx: ToolContext, counters: Counters
    ) -> ChallengeReport:
        findings = []
        for f in state.findings:
            c = self.candidates[f.finding_id]
            findings.append(
                ChallengeFinding(
                    finding_id=c.finding_id,
                    kind=c.kind,
                    episode_id=c.episode_id,
                    rule_ids=c.rule_ids,
                    alert_ids=c.alert_ids,
                    facts=dict(c.facts),
                    summary=f"{c.kind.value}: {f.note or 'pending human review'}",
                    status=f.status,
                    run_id=run_id,
                )
            )
        return ChallengeReport(
            run_id=run_id, snapshot_id=ctx.snapshot.snapshot_id, findings=tuple(findings)
        )
