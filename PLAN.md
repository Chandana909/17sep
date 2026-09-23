# PLAN — build order

Each phase ends green on `make check`, audited by `invariant-auditor`, and recorded in `PROGRESS.md`.

## Phase 0 — Foundation ✅
Specs: `specs/field-contract.md`, `specs/agents.md`.
Scope: repo facts; field contract; typed models; PIT read-only source; linking; verification; evidence; treatment; priority; cohorts; reports; agent gateway/narrator/manifest; invariant test suite.
Acceptance: all 16 rails covered by at least one test; agents-disabled equivalence test passes.

## Phase 1 — Real data adapter
Specs: `specs/field-contract.md`.
Scope: `SqlReadOnlySource` behind `ReadOnlySource`. It uses parameterised SELECT with `record_time <= :as_of` pushed down and a SELECT-only role. Map real SCP/CAL column names through a new alias mapping version.
Invariant tests first: the adapter issues only SELECT; every query binds `as_of`; unknown columns raise.
Acceptance: golden archetypes reproduce from a fixture DB.

## Phase 2 — Supervisor output surface
Scope: export of decisions, reports, RFI drafts and manifests (JSONL/CSV) to an ASAS-owned store, never to SCP/CAL. Add a CLI entry point `python -m asas --as-of ...`.
Invariant tests first: the output path is never an SCP/CAL connection; exports are byte-stable.

## Phase 3 — Production ModelGateway
Specs: `specs/agents.md`.
Scope: a real LLM adapter behind `ModelGateway`, with prompt versioning and a manifest store. Also optional category *suggestions* recorded only in prose annotations.
Invariant tests first: rail 15 equivalence runs against the real adapter in record/replay mode.

## Phase 4 — Additional claim checkers
Scope: new claim types, each a pure checker plus config keys plus golden archetypes. Each needs a contract field before it can ever be VERIFIED.
