# PLAN: build order

Each phase ends green on `make check`, audited by `invariant-auditor`, and recorded in `PROGRESS.md`.

## Phase 0: Foundation ✅
Field contract, typed models, PIT read-only source, linking, verification, evidence, treatment, priority, cohorts, reports, agent gateway/narrator/manifest, invariant suite.

## Phase 1: Data integration, loosely coupled ✅
Versioned `SourceMapping` (TOML) is the only place real column names live. It provides renames, value tables, timestamp offset/format, optional pure transform hooks, and file-level schema-drift detection. `CsvReader` and `SqlReader` (generated PIT `SELECT` only, identifier allow-list, read-only SQLite) sit behind `MappedSource`. Coverage flags cover unmapped RFI/history sources. Sample data uses real SCP column names.

## Phase 2: Supervisor output surface ✅
`asas` CLI: `run`, `check-mapping`, `check-config`, `init-mapping`. It writes byte-stable output files to an ASAS-owned directory and refuses to write into source locations. CSV formula-injection is neutralised.

## Phase 3: Production ModelGateway ✅
OpenAI-compatible gateway (Qwen via Ollama/vLLM/LM Studio/DashScope, or hosted models), built from config only. Weak-model hardening: `<think>`/fence stripping and draft-rewrite prompting. Record/replay gateways support audits and rail 15 checks against real model text.

## Phase 4: Business extensibility ✅
Claim-checker plugin registry (`src/asas/checkers/`), evidence-item registry, `QUANTITY_CORRECTION` claim, rebook matching on side and `ORIGINAL_TRADE_ID`, and `check-config` validation of business edits. Guides: `docs/INTEGRATION.md`, `docs/BUSINESS_CHANGES.md`.

## Next (needs business input; see PROGRESS UNKNOWNs)
- Confirm real identifier semantics per `SOURCE` (TRADE_ID vs ALTERNATE_TRADE_ID vs URN_REF) before cross-trade linking.
- Real claim taxonomy per alert type; entitlement model for person fields.
