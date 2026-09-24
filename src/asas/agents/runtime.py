"""Agent runtime: the loop every agent program runs in.

Responsibilities: idempotent runs (replay-by-record of completed runs), checkpoint after every
step and resume after a crash, budgets (steps, tool calls, model calls, wall time), model calls
with schema validation, one repair attempt and deterministic playbook fallback, tool
execution through the closed registry, tracing, and a manifest pinning the full decision
environment (agent, prompt, model, tool schema, config, snapshot, policy, graph, memory).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from asas.agents.gateway import ModelGateway, ModelRequest
from asas.agents.prompts import PromptTemplate
from asas.agents.tools import TOOL_SCHEMA_VERSION, ToolContext, ToolExecutor, ToolResult
from asas.agents.validation import clean_model_text
from asas.core.config import Config
from asas.core.errors import ModelError
from asas.core.ids import canonical_json, content_hash, stable_id
from asas.core.logging import get_logger, log_event
from asas.core.tracing import NOOP_TRACER, Tracer
from asas.store.db import Store, ts_key, utcnow

S = TypeVar("S", bound=BaseModel)
R = TypeVar("R", bound=BaseModel)
_log = get_logger("runtime")


class ActionRejected(ValueError):
    """The model's reply is not a valid action for this program."""


class Budget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_steps: int
    max_tool_calls: int
    max_model_calls: int
    max_seconds: float

    @staticmethod
    def from_config(cfg: Config, agent: str) -> Budget:
        section = cfg.section("agents", "budgets", agent)
        return Budget(
            max_steps=int(section["max_steps"]),
            max_tool_calls=int(section["max_tool_calls"]),
            max_model_calls=int(section["max_model_calls"]),
            max_seconds=float(section["max_seconds"]),
        )


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str
    idempotency_key: str
    agent: str
    agent_version: str
    subject: str
    prompt_id: str
    prompt_version: str
    prompt_digest: str
    model_id: str
    policy_mode: str
    tool_schema_version: str
    config_version: str
    config_fingerprint: str
    snapshot_id: str
    policy_bundle_id: str
    as_of: str
    principal: str
    extra: dict[str, str]


class Counters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    fallbacks: int = 0
    model_errors: list[str] = []


class AgentProgram(Protocol[S, R]):
    name: str
    version: str
    prompt: PromptTemplate
    tools: frozenset[str]
    state_type: type[S]
    result_type: type[R]

    def start(self, subject: str, ctx: ToolContext) -> S: ...
    def playbook(self, state: S, ctx: ToolContext) -> dict[str, Any]: ...
    def observe(self, state: S, ctx: ToolContext) -> dict[str, Any]: ...
    def parse(self, action: dict[str, Any]) -> dict[str, Any]: ...
    def apply(
        self, state: S, action: dict[str, Any], executor: ToolExecutor, step: int, ctx: ToolContext
    ) -> tuple[S, dict[str, Any]]: ...
    def done(self, state: S) -> bool: ...
    def exhaust(self, state: S, reason: str) -> S: ...
    def finalize(self, state: S, run_id: str, ctx: ToolContext, counters: Counters) -> R: ...


@dataclass(frozen=True)
class RunOutcome(Generic[R]):
    result: R
    manifest: RunManifest
    replayed: bool
    resumed: bool
    counters: Counters


class AgentRuntime:
    def __init__(
        self,
        cfg: Config,
        store: Store | None,
        gateway: ModelGateway | None,
        tracer: Tracer = NOOP_TRACER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.tracer = tracer
        self._clock = clock

    @property
    def llm_enabled(self) -> bool:
        return self.gateway is not None and self.cfg.boolean("agents", "enabled")

    def run(
        self,
        program: AgentProgram[S, R],
        subject: str,
        ctx: ToolContext,
        extra: dict[str, str] | None = None,
    ) -> RunOutcome[R]:
        mode = "llm" if self.llm_enabled else "playbook"
        model_id = self.gateway.model_id if (self.llm_enabled and self.gateway) else "none"
        key = content_hash(
            [
                program.name,
                program.version,
                program.prompt.digest,
                subject,
                ctx.snapshot.snapshot_id,
                self.cfg.fingerprint,
                model_id,
                TOOL_SCHEMA_VERSION,
                sorted(ctx.principal.desks),
                ctx.principal.person_data,
                extra or {},
            ]
        )
        run_id = stable_id("RUN", key)
        manifest = RunManifest(
            run_id=run_id,
            idempotency_key=key,
            agent=program.name,
            agent_version=program.version,
            subject=subject,
            prompt_id=program.prompt.prompt_id,
            prompt_version=program.prompt.version,
            prompt_digest=program.prompt.digest,
            model_id=model_id,
            policy_mode=mode,
            tool_schema_version=TOOL_SCHEMA_VERSION,
            config_version=self.cfg.version,
            config_fingerprint=self.cfg.fingerprint,
            snapshot_id=ctx.snapshot.snapshot_id,
            policy_bundle_id=ctx.snapshot.policy.bundle_id,
            as_of=ctx.snapshot.as_of.isoformat(),
            principal=ctx.principal.user_id,
            extra=extra or {},
        )
        with self.tracer.span("agent.run", agent=program.name, run_id=run_id, subject=subject):
            recorded = self._completed(run_id, program)
            if recorded is not None:
                log_event(_log, "agent.replayed", run_id=run_id, agent=program.name)
                return RunOutcome(
                    recorded, manifest, replayed=True, resumed=False, counters=Counters()
                )
            state, counters, resumed = self._resume_or_start(program, subject, ctx, manifest)
            executor = ToolExecutor(
                ctx=ctx,
                allowed=program.tools,
                run_id=run_id,
                recorder=self._record_tool(run_id),
                tracer=self.tracer,
            )
            budget = Budget.from_config(self.cfg, program.name)
            started = self._clock()
            while not program.done(state):
                exhausted = self._exhausted(budget, counters, started)
                if exhausted:
                    state = program.exhaust(state, exhausted)
                    break
                action, policy = self._next_action(program, state, ctx, budget, counters)
                with self.tracer.span(
                    "agent.step",
                    step=counters.steps,
                    policy=policy,
                    action=str(action.get("action")),
                ):
                    state, observation = program.apply(state, action, executor, counters.steps, ctx)
                counters.steps += 1
                counters.tool_calls += int(observation.get("tool_call", 0))
                self._checkpoint(run_id, counters, policy, action, observation, state)
            result = program.finalize(state, run_id, ctx, counters)
            self._finish(run_id, result)
            log_event(
                _log,
                "agent.completed",
                run_id=run_id,
                agent=program.name,
                steps=counters.steps,
                tool_calls=counters.tool_calls,
                model_calls=counters.model_calls,
                fallbacks=counters.fallbacks,
            )
            return RunOutcome(result, manifest, replayed=False, resumed=resumed, counters=counters)

    # ------------------------------------------------------------ decisions

    def _next_action(
        self,
        program: AgentProgram[S, R],
        state: S,
        ctx: ToolContext,
        budget: Budget,
        counters: Counters,
    ) -> tuple[dict[str, Any], str]:
        if not self.llm_enabled or self.gateway is None:
            return program.playbook(state, ctx), "playbook"
        if counters.model_calls >= budget.max_model_calls:
            counters.fallbacks += 1
            return program.playbook(state, ctx), "playbook-budget"
        payload = program.observe(state, ctx)
        user = canonical_json(payload)
        limit = self.cfg.integer("agents", "max_prompt_chars")
        if len(user) > limit:
            user = user[:limit]
        error = ""
        for attempt in range(2):
            request = ModelRequest(
                model_id=self.gateway.model_id,
                prompt_id=program.prompt.prompt_id,
                prompt_version=program.prompt.version,
                system=program.prompt.system,
                user=user if not error else f"{user}\nYOUR PREVIOUS REPLY WAS INVALID: {error}",
                max_tokens=self.cfg.integer("agents", "max_output_tokens"),
            )
            try:
                counters.model_calls += 1
                reply = self.gateway.complete(request)
                action = json.loads(clean_model_text(reply.text))
                if not isinstance(action, dict):
                    raise ActionRejected("reply is not a JSON object")
                return program.parse(action), "llm"
            except ModelError as exc:
                error = f"model:{exc}"
                break
            except (json.JSONDecodeError, ValidationError, ActionRejected) as exc:
                error = f"invalid:{type(exc).__name__}"
                if attempt == 0 and counters.model_calls < budget.max_model_calls:
                    continue
                break
        counters.fallbacks += 1
        counters.model_errors.append(error)
        log_event(_log, "agent.fallback", agent=program.name, reason=error)
        return program.playbook(state, ctx), "playbook-fallback"

    def _exhausted(self, budget: Budget, counters: Counters, started: float) -> str:
        if counters.steps >= budget.max_steps:
            return "BUDGET_STEPS"
        if counters.tool_calls >= budget.max_tool_calls:
            return "BUDGET_TOOL_CALLS"
        if self._clock() - started > budget.max_seconds:
            return "BUDGET_TIME"
        return ""

    # ------------------------------------------------------------ persistence

    def _completed(self, run_id: str, program: AgentProgram[S, R]) -> R | None:
        if self.store is None:
            return None
        rows = self.store.query("SELECT result FROM agent_run_results WHERE run_id = ?", (run_id,))
        return program.result_type.model_validate_json(rows[0][0]) if rows else None

    def _resume_or_start(
        self, program: AgentProgram[S, R], subject: str, ctx: ToolContext, manifest: RunManifest
    ) -> tuple[S, Counters, bool]:
        if self.store is not None:
            started = self.store.query(
                "SELECT run_id FROM agent_runs WHERE run_id = ?", (manifest.run_id,)
            )
            if started:
                rows = self.store.query(
                    "SELECT state FROM agent_steps WHERE run_id = ? ORDER BY step DESC LIMIT 1",
                    (manifest.run_id,),
                )
                if rows:
                    envelope = json.loads(rows[0][0])
                    log_event(_log, "agent.resumed", run_id=manifest.run_id)
                    return (
                        program.state_type.model_validate(envelope["state"]),
                        Counters.model_validate(envelope["counters"]),
                        True,
                    )
                return program.start(subject, ctx), Counters(), True
            self.store.insert(
                "agent_runs",
                {
                    "run_id": manifest.run_id,
                    "idempotency_key": manifest.idempotency_key,
                    "agent": manifest.agent,
                    "subject": subject,
                    "manifest": manifest.model_dump_json(),
                    "started_at": ts_key(utcnow()),
                },
            )
        return program.start(subject, ctx), Counters(), False

    def _checkpoint(
        self,
        run_id: str,
        counters: Counters,
        policy: str,
        action: dict[str, Any],
        observation: dict[str, Any],
        state: BaseModel,
    ) -> None:
        if self.store is None:
            return
        self.store.insert(
            "agent_steps",
            {
                "run_id": run_id,
                "step": counters.steps,
                "policy": policy,
                "action": canonical_json(action),
                "observation": canonical_json(observation),
                "state": canonical_json(
                    {
                        "state": state.model_dump(mode="json"),
                        "counters": counters.model_dump(mode="json"),
                    }
                ),
                "created_at": ts_key(utcnow()),
            },
        )

    def _record_tool(self, run_id: str) -> Callable[[ToolResult, int], None] | None:
        if self.store is None:
            return None
        store = self.store

        def record(result: ToolResult, step: int) -> None:
            store.insert_ignore(
                "tool_calls",
                {
                    "call_id": result.call_id,
                    "run_id": run_id,
                    "step": step,
                    "tool": result.tool,
                    "args": canonical_json(result.args),
                    "result": canonical_json(result.output)
                    if result.output is not None
                    else "null",
                    "result_hash": content_hash(result.output or {}),
                    "cached": int(result.cached),
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                    "created_at": ts_key(utcnow()),
                },
            )

        return record

    def _finish(self, run_id: str, result: BaseModel) -> None:
        if self.store is None:
            return
        payload = result.model_dump_json()
        self.store.insert(
            "agent_run_results",
            {
                "run_id": run_id,
                "status": "COMPLETED",
                "finished_at": ts_key(utcnow()),
                "result": payload,
                "result_hash": content_hash(payload),
            },
        )
