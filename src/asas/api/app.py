"""FastAPI application: the only interface the UI and external callers use.

Authentication is pluggable: `security.dev_auth = true` reads the principal from headers
(X-User, X-Roles, X-Desks, X-Person-Data) for local use; in production put an OIDC proxy in
front and map its claims to the same headers. Authorisation is enforced in the services.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from asas.core.errors import (
    AsasError,
    DataContractError,
    GovernanceError,
    PermissionDenied,
    ToolError,
)
from asas.core.security import Principal, Role
from asas.domain.models import OutcomeLabel
from asas.engine import evidence as ev
from asas.engine.graph import neighborhood
from asas.services.platform import Platform

STATIC = Path(__file__).parent / "static"


def _json(obj: Any) -> JSONResponse:
    if isinstance(obj, BaseModel):
        return JSONResponse(json.loads(obj.model_dump_json()))
    if isinstance(obj, list):
        return JSONResponse(
            [json.loads(o.model_dump_json()) if isinstance(o, BaseModel) else o for o in obj]
        )
    return JSONResponse(obj)


class DecisionBody(BaseModel):
    label: OutcomeLabel


class NoteBody(BaseModel):
    note: str = ""


class RollbackBody(BaseModel):
    bundle_id: str
    reason: str


class RunBody(BaseModel):
    as_of: datetime | None = None


def create_app(platform: Platform) -> FastAPI:
    app = FastAPI(
        title="ASAS",
        version="2.0.0",
        description="Auditable agentic surveillance: investigate, challenge, discover, evolve.",
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(AsasError)
    async def _errors(_: Request, exc: AsasError) -> JSONResponse:
        status = 400
        if isinstance(exc, PermissionDenied):
            status = 403
        elif isinstance(exc, GovernanceError):
            status = 409
        elif isinstance(exc, DataContractError):
            status = 422
        elif isinstance(exc, ToolError):
            status = 400
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    def principal(request: Request) -> Principal:
        if not platform.cfg.boolean("security", "dev_auth"):
            raise HTTPException(401, "authentication proxy required")
        roles = frozenset(
            Role(r.strip())
            for r in request.headers.get("X-Roles", "viewer").split(",")
            if r.strip() in Role._value2member_map_
        )
        desks = frozenset(
            d.strip() for d in request.headers.get("X-Desks", "*").split(",") if d.strip()
        )
        return Principal(
            user_id=request.headers.get("X-User", "anonymous"),
            roles=roles or frozenset({Role.VIEWER}),
            desks=desks or frozenset({"*"}),
            person_data=request.headers.get("X-Person-Data", "false") == "true",
        )

    def as_of(value: datetime | None = None) -> datetime:
        if value is not None:
            return value
        run = platform.latest_run()
        if run is not None:
            return run.as_of
        watermark = platform.store.data_watermark()
        if watermark is None:
            raise HTTPException(409, "no data ingested yet")
        return watermark

    def visible(p: Principal, desk: str) -> bool:
        return p.sees_desk(desk)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        ok, entries = platform.store.verify_audit_chain()
        return {"status": "ok", "audit_chain_valid": ok, "audit_entries": entries}

    @app.get("/api/overview")
    def overview(p: Principal = Depends(principal)) -> JSONResponse:
        run = platform.latest_run()
        bundle = platform.governance.active_bundle_or_none()
        ok, entries = platform.store.verify_audit_chain()
        candidates = platform.governance.candidates()
        return _json(
            {
                "latest_run": json.loads(run.model_dump_json()) if run else None,
                "active_bundle": bundle.bundle_id if bundle else None,
                "rules": len(bundle.ruleset.rules) if bundle else 0,
                "bulk_scopes": [s.model_dump() for s in bundle.bulk_policy.allowed]
                if bundle
                else [],
                "audit": {"valid": ok, "entries": entries},
                "candidates": {
                    c.candidate_id: platform.governance.state(c.candidate_id).value
                    for c in candidates
                },
                "link_proposals": {
                    s: sum(1 for x in platform.link_proposals() if x.status.value == s)
                    for s in ("VERIFIED", "CANONICAL", "REJECTED")
                },
                "agents": {
                    "llm_enabled": platform.runtime.llm_enabled,
                    "model": platform.cfg.string("agents", "model_id"),
                },
                "user": p.user_id,
            }
        )

    @app.post("/api/pipeline/run")
    def run_pipeline(body: RunBody, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.run_pipeline(as_of(body.as_of), p))

    @app.post("/api/links/resolve")
    def resolve_links(body: RunBody, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.resolve_links(as_of(body.as_of), p))

    @app.get("/api/cases")
    def cases(recommendation: str | None = None, p: Principal = Depends(principal)) -> JSONResponse:
        out = []
        for c in platform.cases():
            if not visible(p, c.desk) or (
                recommendation and c.recommendation.value != recommendation
            ):
                continue
            out.append(
                {
                    "case_id": c.case_id,
                    "episode_id": c.episode_id,
                    "desk": c.desk,
                    "recommendation": c.recommendation.value,
                    "conclusion": c.conclusion,
                    "reasons": list(c.reasons),
                    "score": str(c.score.total),
                    "band": c.score.band,
                    "bucket": c.score.bucket,
                    "alerts": len(c.alert_ids),
                    "cohort_id": c.cohort_id,
                    "control_sample": c.control_sample,
                }
            )
        return _json(out)

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        case = platform.case(case_id)
        if not visible(p, case.desk):
            raise PermissionDenied(f"{p.user_id} is not entitled to desk {case.desk}")
        snap = platform.snapshot(case.as_of)
        investigation = (
            platform.investigation(case.investigation_run_id) if case.investigation_run_id else None
        )
        graph = neighborhood(
            platform.graph(case.as_of),
            f"episode:{case.episode_id}",
            2,
            platform.cfg.integer("tools", "graph_max_nodes"),
        )
        alerts = [ev.alert_view(snap, a).model_dump(mode="json") for a in case.alert_ids]
        return _json(
            {
                "case": json.loads(case.model_dump_json()),
                "episode": ev.episode_view(snap, case.episode_id).model_dump(mode="json"),
                "sequence": ev.event_sequence(snap, case.episode_id).model_dump(mode="json"),
                "alerts": alerts,
                "investigation": json.loads(investigation.model_dump_json())
                if investigation
                else None,
                "graph": graph.model_dump(mode="json"),
            }
        )

    @app.post("/api/cases/{case_id}/investigate")
    def investigate(case_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        case = platform.case(case_id)
        return _json(platform.investigate(case.episode_id, case.as_of, p))

    @app.post("/api/cases/{case_id}/decision")
    def decision(
        case_id: str, body: DecisionBody, p: Principal = Depends(principal)
    ) -> JSONResponse:
        case = platform.case(case_id)
        return _json(platform.record_decision(case_id, body.label, p, case.as_of))

    @app.post("/api/outcomes/{outcome_id}/curate")
    def curate(outcome_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.curate(outcome_id, p))

    @app.get("/api/runs/{run_id}")
    def run_trace(run_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.run_trace(run_id))

    @app.get("/api/links")
    def links(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.link_proposals())

    @app.post("/api/links/{proposal_id}/confirm")
    def confirm_link(proposal_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.confirm_link(proposal_id, p))

    @app.post("/api/challenge")
    def challenge(body: RunBody, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.challenge(as_of(body.as_of), p))

    @app.get("/api/challenge")
    def latest_challenge(p: Principal = Depends(principal)) -> JSONResponse:
        payload = platform.store.get_artifact("challenge_report", "latest")
        return _json(json.loads(payload) if payload else {"findings": []})

    @app.post("/api/discover")
    def discover(body: RunBody, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.discover(as_of(body.as_of), p))

    @app.get("/api/patterns")
    def patterns(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.patterns())

    @app.get("/api/candidates")
    def candidates(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(
            [
                {
                    "candidate": json.loads(c.model_dump_json()),
                    "state": platform.governance.state(c.candidate_id).value,
                }
                for c in platform.governance.candidates()
            ]
        )

    @app.get("/api/candidates/{candidate_id}")
    def candidate(candidate_id: str, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.candidate_view(candidate_id))

    @app.post("/api/candidates/{candidate_id}/{step}")
    def candidate_step(
        candidate_id: str, step: str, body: NoteBody, p: Principal = Depends(principal)
    ) -> JSONResponse:
        when = as_of(None)
        if step == "replay":
            return _json(platform.replay_candidate(candidate_id, when, p))
        if step == "counterexamples":
            return _json(platform.attack_candidate(candidate_id, when, p))
        if step == "shadow":
            report = platform.shadow_candidate(candidate_id, when, p)
            return _json(json.loads(report.model_copy(update={"decisions": ()}).model_dump_json()))
        if step == "submit":
            return _json({"state": platform.submit(candidate_id, p, body.note).value})
        if step == "approve":
            return _json(platform.approve(candidate_id, p, body.note))
        if step == "reject":
            return _json({"state": platform.reject(candidate_id, p, body.note or "rejected").value})
        raise HTTPException(404, f"unknown step {step}")

    @app.get("/api/policy")
    def policy(p: Principal = Depends(principal)) -> JSONResponse:
        active = platform.governance.active_bundle_or_none()
        return _json(
            {
                "active": json.loads(active.model_dump_json()) if active else None,
                "bundles": [
                    {
                        "bundle_id": b.bundle_id,
                        "version": b.version,
                        "parent_id": b.parent_id,
                        "created_by": b.created_by,
                        "source_candidate": b.source_candidate,
                        "notes": b.notes,
                        "rules": len(b.ruleset.rules),
                        "bulk_scopes": len(b.bulk_policy.allowed),
                    }
                    for b in platform.governance.bundles()
                ],
                "activations": platform.governance.activations(),
            }
        )

    @app.post("/api/policy/rollback")
    def rollback(body: RollbackBody, p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.rollback(body.bundle_id, p, body.reason))

    @app.get("/api/graph/{node_id:path}")
    def graph(node_id: str, depth: int = 1, p: Principal = Depends(principal)) -> JSONResponse:
        when = as_of(None)
        return _json(
            neighborhood(
                platform.graph(when),
                node_id,
                min(depth, platform.cfg.integer("tools", "graph_max_depth")),
                platform.cfg.integer("tools", "graph_max_nodes"),
            )
        )

    @app.get("/api/audit")
    def audit(limit: int = 100, p: Principal = Depends(principal)) -> JSONResponse:
        ok, entries = platform.store.verify_audit_chain()
        return _json(
            {
                "valid": ok,
                "entries": entries,
                "recent": platform.store.audit_entries(min(limit, 500)),
            }
        )

    @app.get("/api/evaluation")
    def evaluation(p: Principal = Depends(principal)) -> JSONResponse:
        payload = platform.store.get_artifact("evaluation", "latest")
        return _json(json.loads(payload) if payload else {})

    return app
