"""FastAPI application: the only interface the UI and external callers use.

Identity comes from the configured authenticator (core/auth.py): dev headers locally, an
authenticating proxy, or OIDC bearer tokens. With `security.environment = "prod"` the app
refuses to start when a readiness check fails (core/readiness.py). Every response carries
security headers and a request id; bodies are size-limited; each principal is rate-limited.
Authorisation (roles, desks, four-eyes) is enforced in the services, never in the UI.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from asas.core import metrics
from asas.core.auth import Authenticator, AuthError, EntitlementError, build_authenticator
from asas.core.errors import (
    AsasError,
    DataContractError,
    GovernanceError,
    PermissionDenied,
    ToolError,
)
from asas.core.logging import get_logger, log_event
from asas.core.ratelimit import RateLimiter
from asas.core.readiness import enforce
from asas.core.security import Principal
from asas.domain.models import Case, OutcomeLabel
from asas.engine import evidence as ev
from asas.engine.graph import neighborhood
from asas.services.integration import capability_matrix
from asas.services.platform import Platform

STATIC = Path(__file__).parent / "static"
_log = get_logger("api")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


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


class OpsBody(BaseModel):
    bulk_suspended: bool
    llm_suspended: bool
    reason: str


def create_app(platform: Platform, authenticator: Authenticator | None = None) -> FastAPI:
    cfg = platform.cfg
    readiness = enforce(cfg, platform.database_url)  # prod + any FAIL: refuse to start
    auth = authenticator or build_authenticator(cfg)
    limiter = RateLimiter(cfg.integer("security", "rate_limit_per_minute"))
    max_body = cfg.integer("security", "max_body_bytes")
    prod = cfg.string("security", "environment") == "prod"
    app = FastAPI(
        title="ASAS",
        version="2.1.0",
        description="Auditable agentic surveillance: investigate, challenge, discover, evolve.",
    )
    app.state.readiness = readiness
    metrics.BUILD.labels("2.1.0", cfg.version).set(1)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def _envelope(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        started = time.perf_counter()
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > max_body:
            response: Response = JSONResponse(
                {"error": "PayloadTooLarge", "detail": f"body over {max_body} bytes"},
                status_code=413,
            )
        else:
            response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        metrics.HTTP_REQUESTS.labels(request.method, template, str(response.status_code)).inc()
        metrics.HTTP_SECONDS.labels(template).observe(time.perf_counter() - started)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if prod:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        log_event(
            _log,
            "http.request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    @app.exception_handler(AsasError)
    async def _errors(_: Request, exc: AsasError) -> JSONResponse:
        status = 400
        if isinstance(exc, AuthError):
            metrics.AUTH_FAILURES.labels(auth.mode).inc()
            return JSONResponse(
                {"error": "AuthError", "detail": str(exc)},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"} if auth.mode == "oidc" else {},
            )
        if isinstance(exc, PermissionDenied | EntitlementError):
            status = 403
        elif isinstance(exc, GovernanceError):
            status = 409
        elif isinstance(exc, DataContractError):
            status = 422
        elif isinstance(exc, ToolError):
            status = 400
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=status)

    def principal(request: Request) -> Principal:
        who = auth.authenticate(request.headers)
        allowed, retry = limiter.allow(who.user_id)
        if not allowed:
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": str(int(retry) + 1)}
            )
        return who

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

    def _case_context(case: Case) -> dict[str, Any]:
        """Business meaning of everything shown on a case (config/business_context.toml)."""
        ctx = platform.context
        cls = case.classification
        signals = [d.signal for d in case.deviation.deviations] if case.deviation else []
        if case.deviation and case.deviation.joint:
            signals.append("joint")
        return {
            "version": ctx.version,
            "conclusion": ctx.entry("hypotheses", case.conclusion) if case.conclusion else None,
            "recommendation": ctx.meaning("recommendations", case.recommendation.value),
            "category": ctx.meaning("categories", cls.category.value) if cls else "",
            "reasons": {r: ctx.meaning("reasons", r) for r in case.reasons},
            "corroboration": {c: ctx.meaning("corroboration", c) for c in cls.corroboration}
            if cls
            else {},
            "signals": {s: ctx.entry("signals", s) for s in signals},
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/whoami")
    def whoami(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(
            {
                "user": p.user_id,
                "roles": sorted(r.value for r in p.roles),
                "desks": sorted(p.desks),
                "person_data": p.person_data,
                "auth_mode": auth.mode,
                "environment": cfg.string("security", "environment"),
            }
        )

    ready_cache: dict[str, Any] = {"at": 0.0, "audit": (False, 0)}

    @app.get("/api/health/live")
    def live() -> dict[str, str]:
        """Liveness: the process answers. Never touches dependencies."""
        return {"status": "alive"}

    @app.get("/api/health/ready")
    def ready() -> JSONResponse:
        """Readiness: storage reachable, a policy is active, the audit chain verifies."""
        problems: list[str] = []
        try:
            platform.store.audit_head()
        except Exception as exc:
            problems.append(f"storage: {type(exc).__name__}")
        if not problems:
            if platform.governance.active_bundle_or_none() is None:
                problems.append("policy: no active bundle")
            max_age = cfg.integer("observability", "ready_cache_seconds")
            if time.monotonic() - ready_cache["at"] > max_age:
                ready_cache["audit"] = platform.store.verify_audit_chain()
                ready_cache["at"] = time.monotonic()
            ok, entries = ready_cache["audit"]
            metrics.AUDIT_VALID.set(1 if ok else 0)
            metrics.AUDIT_ENTRIES.set(entries)
            if not ok:
                problems.append("audit: chain does not verify")
        body = {"status": "ready" if not problems else "not_ready", "problems": problems}
        return JSONResponse(body, status_code=200 if not problems else 503)

    if cfg.boolean("observability", "metrics_enabled"):

        @app.get("/metrics")
        def prometheus() -> Response:
            return Response(metrics.exposition(), media_type="text/plain; version=0.0.4")

    @app.get("/api/monitoring/drift")
    def monitoring(p: Principal = Depends(principal)) -> JSONResponse:
        report = platform.drift_report()
        return _json(report if report is not None else {"status": "NO_RUNS", "flags": []})

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
                "ops": platform.ops.state().model_dump(mode="json"),
                "drift": (lambda d: d.model_dump(mode="json") if d else None)(
                    platform.drift_report()
                ),
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
                    "category": c.classification.category.value if c.classification else None,
                    "severity": c.classification.severity if c.classification else None,
                    "confidence": c.classification.confidence if c.classification else None,
                    "residual_band": c.classification.residual_band if c.classification else None,
                    "outlyingness": str(c.classification.outlyingness)
                    if c.classification
                    else None,
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
                "context": _case_context(case),
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

    @app.get("/api/ops")
    def ops_state(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(
            {
                "state": platform.ops.state().model_dump(mode="json"),
                "history": [s.model_dump(mode="json") for s in platform.ops.history()[-20:]],
            }
        )

    @app.post("/api/ops")
    def set_ops_state(body: OpsBody, p: Principal = Depends(principal)) -> JSONResponse:
        state = platform.ops.set(
            p,
            bulk_suspended=body.bulk_suspended,
            llm_suspended=body.llm_suspended,
            reason=body.reason,
        )
        return _json(state.model_dump(mode="json"))

    @app.get("/api/data/capabilities")
    def data_capabilities(p: Principal = Depends(principal)) -> JSONResponse:
        """What the loaded data supports, from the same capability model the engine uses."""
        snap = platform.snapshot(as_of(None))
        run = platform.latest_run()
        return _json(
            {
                "as_of": snap.as_of.isoformat(),
                "matrix": capability_matrix(snap.source, cfg, snap.policy.ruleset.rules),
                "latest_run": {
                    "degraded": run.degraded,
                    "data_gates": list(run.data_gates),
                    "unavailable_fields": list(run.unavailable_fields),
                    "rule_gaps": list(run.rule_gaps),
                }
                if run
                else None,
            }
        )

    @app.get("/api/context")
    def business_context(p: Principal = Depends(principal)) -> JSONResponse:
        return _json(platform.context.to_dict())

    @app.get("/api/evaluation")
    def evaluation(p: Principal = Depends(principal)) -> JSONResponse:
        payload = platform.store.get_artifact("evaluation", "latest")
        return _json(json.loads(payload) if payload else {})

    return app
