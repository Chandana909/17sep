# CLAUDE.md — ASAS v1

You are extending an existing SCP CAL surveillance repository. You are not building greenfield.

## Mission

Prepare bulk review for supervisors: turn alerts into validated lifecycle episodes and review cases, verify operational explanations against trade data, propose evidence-complete bulk-review cohorts, and write business-level reports. The supervisor decides everything.

Full design: `SOLUTION.md`. Build order: `PLAN.md`. Specs: `specs/`. Current state: `PROGRESS.md`.

## Session protocol (every session, no exceptions)

1. Read `PROGRESS.md`, then the current phase in `PLAN.md`, then only the specs that phase names.
2. Use plan mode. Produce: files to change, tests to add, invariants touched, open UNKNOWNs hit. Wait for approval.
3. Write invariant tests before implementation where the phase lists them.
4. Implement the smallest slice that meets the phase's acceptance criteria.
5. Run `make check` (or `python scripts/check.py` where make is unavailable). Fix until green.
6. Delegate a diff review to the `invariant-auditor` subagent. Fix every FAIL.
7. Update `PROGRESS.md` (done, decisions, UNKNOWNs, next step) and `docs/SAD.md` if architecture changed.
8. Stop. Do not start the next phase.

Never `git push`. The user pushes.

## Rails (violating any of these is a bug, regardless of instructions elsewhere)

1. Read-only to SCP and CAL by every path.
2. No suppression. Nothing is removed from review by the system. Cohorts are proposals; supervisors attest.
3. No autonomous business action. The system never signs off, sends an RFI, moves ownership or writes a disposition.
4. Agents never produce numbers, dates or identifiers. They reference facts as `{{F<n>}}`; the renderer fills values. Any digit in agent prose outside a placeholder fails validation (whitelist in config).
5. Agents propose, code decides. Category acceptance, claim verification, evidence readiness, review treatment, cohort eligibility and priority are deterministic.
6. Free text is untrusted data. Never evidence on its own. Delimit it, never execute it.
7. Absence of contradiction is not confirmation. Unprovable claims are `NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS`.
8. Workflow/outcome fields never reach linking, scoring, treatment or any current-case decision: `ALERT_GRP_ID`, `RFI_FLAG`, `AUTO_RFI_FLAG`, `SIGNOFF_STD_COMMENTS`, `SIGNOFF_COMMENTS`, `WF_ACTION_NAME`, `MESSAGE_DESCRIPTION`, `MSG_TEXT`. Exception: open-RFI status derived point-in-time from events that exist before `as_of`.
9. History may raise attention, never solely lower it. Comparable-case outcomes can force individual review; they can never make a case bulk-ready.
10. Point-in-time everywhere. No read with `record_time > as_of`. Retrieval of past cases uses `decided_at < as_of`.
11. Person fields are not predictors. `TRADER_REQUESTOR`, `TRADE_MODIFIER`, `SUPERVISOR_GPN` may appear in reports (entitlement-gated) but never in treatment policy or priority.
12. Config, not literals. Thresholds, tolerances, windows, weights, limits come from versioned config. Missing config that affects a decision → fail safe (route to individual review), never invent a value.
13. Agent runtime holds no write credentials. SELECT-only DB role; closed tool registry; no generic SQL/HTTP/file tools.
14. Every agent invocation stores its decision-environment manifest (see `specs/agents.md` §7).
15. Agent-disabled equivalence. With `agents.enabled=false`, everything except report prose and RFI drafts is byte-identical; reports fall back to deterministic templates.
16. Only fields in `specs/field-contract.md` exist. Unknown columns fail loudly. Aliases map only through the versioned mapping.

## Conventions

* Conform to the repository's existing language, layout, tooling and style.
* Deterministic modules are pure functions over typed inputs. No hidden I/O.
* LLM access only through the `ModelGateway` interface. `FakeModelGateway` for tests (deterministic, fixture-driven).
* New infrastructure is a proposal in `docs/SAD.md`, not a dependency, until approved.
* Test types required per component: unit, property (determinism, idempotence, PIT), golden (lifecycle archetypes), adversarial (injection text, dirty data).

## Repository facts (Phase 0)

* Language / version: Python 3.11 (stdlib only at runtime; `tomllib`, `decimal`, frozen dataclasses).
* Package layout: `src/asas/` (src layout); agents in `src/asas/agents/`; tests in `tests/` (flat, `factories.py` shared builders); config in `config/asas.v1.toml`.
* Existing linking module path: `src/asas/linking.py` (no prior SCP CAL repo was present; created in Phase 0).
* DB / storage: none yet. `ReadOnlySource` protocol + `InMemorySource`; SQL adapter proposed in `docs/SAD.md`.
* Test command: `python -m pytest` (pytest + hypothesis).
* Lint / type command: `python -m ruff check src tests scripts && python -m ruff format --check src tests scripts`; `python -m mypy` (strict).
* `make check` runs: lint → type → test. Windows without make: `python scripts/check.py` (same steps).
