# CLAUDE.md: ASAS v2.1 (auditable agentic surveillance)

Agents investigate, challenge, discover and propose evolution of detection logic. Deterministic computation, replay, evidence and human governance remain the source of truth.

Architecture: `docs/architecture.md`. Anomaly analysis: `docs/anomaly-analysis.md`. Decisions: `docs/adr/`. Governance: `docs/governance.md`. Security: `docs/security.md`. Deployment and operations: `docs/deployment.md`, `docs/operations.md`. Business changes: `docs/playbooks/`. Evaluation: `docs/evaluation.md`. Coding-agent quick guide: `AGENTS.md`. Original design notes: `docs/archive/`.

## Session protocol
1. Read the relevant docs and code before changing anything; plan first for non-trivial work (use the `understand-and-plan` skill to write `taskNN.md`).
2. Write or extend tests with the change: unit, property, e2e scenario, adversarial.
3. Run `python scripts/check.py` (ruff format, ruff lint, strict mypy, pytest). Fix until green.
4. Delegate a diff review to the `invariant-auditor` subagent. Fix every FAIL.
5. Never `git push` unless the user explicitly asks in the conversation.

## Rails (violating any of these is a bug)
1. **Read-only to SCP/CAL.** The store is append-only (triggers); agents and tools read through `mode=ro` connections and a frozen snapshot.
2. **Agents propose, code decides.** Hypothesis status, adjudication, case treatment, cohorting, link verification, rule validation, replay, counterexamples and shadow are deterministic.
3. **The LLM never invents facts.** No numbers, dates, statistics, relationships or rule results; prose may only cite evidence ids (`[E3]`). All model output is schema-validated.
4. **No autonomous business action.** Nothing signs off, sends an RFI, closes, suppresses or writes a disposition. Cases are recommendations; cohorts await attestation.
5. **Absence of contradiction is not confirmation.** Missing evidence gives INSUFFICIENT, and the case abstains to individual review.
6. **Agent relationships are quarantined.** Proposed → deterministically VERIFIED → canonical only after a human confirms.
7. **Governed evolution only.** Candidate → replay → counterexamples → shadow → human submit → four-eyes approval → immutable versioned bundle.
8. **Point-in-time everywhere.** No read with `record_time > as_of`; labels only from outcomes `decided_at < as_of`.
9. **Only curated outcomes are labels.** History may raise attention (`HISTORY_ADVERSE`), never make a case bulk-eligible.
10. **Workflow and person fields never reach decisions.** They are quarantined in annexes; the trader baseline is entitlement-gated and raise-only.
11. **Config, not literals.** Every threshold is in `config/asas.toml`; missing config fails safe.
12. **Closed tool registry.** Typed, bounded, read-only tools; no generic SQL, HTTP or file tools; similarity is lead-only.
13. **Every agent run is reproducible.** Idempotency key, manifest, per-step checkpoint, tool-call and model-response records.
14. **Only fields in `docs/field-contract.md` exist.** Unknown columns fail loudly; aliases only through a versioned mapping.
15. **Unexplained never goes bulk.** A verified deviation that no SUPPORTED benign explanation covers (`anomaly.explains`) blocks bulk; a field missing from the data makes dependent checks INSUFFICIENT, never contradicted.
16. **Operations can always stop the machine.** Safe mode, data gates and production readiness checks block bulk or start-up; they are audited and never bypassed in code.

## Repository facts
- Python 3.11, pydantic v2, FastAPI, SQLite or PostgreSQL (`store/dialect.py`, versioned migrations), no ML dependencies.
- Layout: `src/asas/{core,data,store,engine,agents,evolution,services,api}`, `tests/`, `config/`, `deploy/`, `docs/`.
- Config: `config/asas.toml` (every threshold) + overlays (`ASAS_CONFIG_OVERLAY`); wording in `config/business_context.toml`.
- Checks: `python scripts/check.py` (or `make check`); PostgreSQL tests use `pgserver` locally or `ASAS_TEST_DATABASE_URL`.
- Demo: `python -m asas demo --db out/asas.db`. Real data: `python -m asas data profile|draft-mapping|check`.
- Tests: `PYTHONPATH=src python -m pytest`; the session fixture runs the full demo once.
- Console palette: red, grey, black and white only; no inline styles or scripts (CSP), enforced by tests.
