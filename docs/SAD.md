# Software Architecture Document — ASAS v1

## Context
ASAS sits beside SCP (alerts/workflow) and CAL (trade lifecycle) as a **read-only** consumer. Its outputs are proposals for supervisors: review cases, cohorts, reports and RFI drafts. It never writes to SCP or CAL.

## Components
- **Deterministic core** (`src/asas/*.py`): pure functions over frozen dataclasses. All decisions live here.
- **Agent runtime** (`src/asas/agents/`): prose only. No I/O imports, a closed tool registry, and manifests per call.
- **Config** (`config/asas.v1.toml`): versioned; every decision parameter lives here.

## Trust boundaries
1. Source → ASAS: field contract, strict parsing, PIT filter plus `assert_pit` defence in depth.
2. Free text → agents: `delimit`, control-character stripping, marker neutralisation.
3. Agents → reports: `validate_prose` and a deterministic fallback.

## Proposals (not dependencies until approved)
- **P1 `SqlReadOnlySource`**: DB-API adapter, SELECT-only DB role, parameterised `WHERE record_time <= :as_of`, column allow-list from the field contract.
- **P2 Manifest/output store**: append-only JSONL or table owned by ASAS.
- **P3 Production `ModelGateway`**: HTTP adapter in a separate process holding only the model API key, with no DB credentials.
