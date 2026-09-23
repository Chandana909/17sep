# Software Architecture Document: ASAS v1

## Context
ASAS sits beside SCP (alerts/workflow) and CAL (trade lifecycle) as a **read-only** consumer. Its outputs are proposals for supervisors: review cases, cohorts, reports and RFI drafts. It never writes to SCP or CAL.

## Layers

```
 real data (CSV / DB)          LLM endpoint (optional)
        │                              │
 adapters/csv_source, sql_source   adapters/openai_compat, replay, gateways
        │  (RowReader)                 │  (ModelGateway)
 mapping.py  ← config/mapping.*.toml   │
        │  contract rows               │
 ingest.py → models (typed, frozen)    │
        │                              │
 sources.MappedSource (PIT)            │
        │                              │
 pipeline.run ── linking → cases(checkers, evidence, treatment, priority) → cohorts
        │                              │
        └── report.py → agents/narrator (prose only, validated) ─┘
        │
 output.py → out/ (ASAS-owned)         cli.py wires it all
```

- **Deterministic core** (`src/asas/*.py`, `src/asas/checkers/`): pure functions over frozen dataclasses. All decisions live here.
- **Agent runtime** (`src/asas/agents/`): prose only. No I/O imports, a closed tool registry, and a manifest per call.
- **Adapters** (`src/asas/adapters/`): all environment coupling. Swapping data sources or models never touches the core.
- **Config**: `config/asas.v1.toml` (decisions) and `config/mapping.*.toml` (data shape), both versioned and both stamped on every output.

## Trust boundaries
1. Source → ASAS: mapping allow-list, strict parsing, PIT filtering plus `assert_pit`. SQL uses generated SELECTs only, identifier validation and bound `as_of`.
2. Free text → agents: `delimit`, control-character stripping, marker neutralisation.
3. Agents → reports: `clean_model_text`, `validate_prose` and a deterministic fallback.
4. ASAS → files: the output directory must not overlap source locations; CSV formula-injection is neutralised.

## Extension points
| Need | Extension point |
|---|---|
| New data source kind | Implement `RowReader` (`columns`, `rows`) |
| New DB | `--db-factory module:function` + `[sql]` options |
| New model provider | Implement `ModelGateway.complete` and add it to `adapters/gateways.py` |
| New claim type | `@register` in `src/asas/checkers/` |
| New evidence item | `@register_evidence` in `src/asas/evidence.py` |

## Accepted proposals
- P1 SQL read-only source: implemented (`adapters/sql_source.py`).
- P2 Output store: implemented as files (`output.py`); a table store would be another writer, never SCP/CAL.
- P3 Production ModelGateway: implemented (`adapters/openai_compat.py`). Deploy it with only a model API key and no DB credentials.
