# CLAUDE.md: ASAS v2 (auditable agentic surveillance)

Agents investigate, challenge, discover and propose evolution of detection logic. Deterministic computation, replay, evidence and human governance remain the source of truth.

Architecture: `docs/architecture.md`. Decisions: `docs/adr/`. Governance: `docs/governance.md`. Evaluation: `docs/evaluation.md`. Original design notes: `docs/archive/`.

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

## Repository facts
- Python 3.11, pydantic v2, FastAPI, SQLite (Postgres-portable SQL), no ML dependencies.
- Layout: `src/asas/{core,data,store,engine,agents,evolution,services,api}`, `tests/`, `config/`, `docs/`.
- Checks: `python scripts/check.py` (or `make check`). Demo: `python -m asas demo --db out/asas.db`.
- Tests: `PYTHONPATH=src python -m pytest`; the session fixture runs the full demo once.
