# ASAS v1 — Solution design

ASAS prepares supervisor bulk review. It reads SCP/CAL point-in-time, and it proposes; supervisors decide.

## Pipeline (`asas.pipeline.run`)

```
ReadOnlySource(as_of) ─► assert_pit ─► link_episodes ─► build_case ─► propose_cohorts ─► reports
  alerts / events /        rail 10      linking.py      cases.py        cohorts.py        report.py
  rfi events / past                     lifecycle        ├ category       min/max size      + agents/narrator
  cases / annexes                       validation       ├ verify claims  (config)          (prose only)
                                                         ├ evidence
                                                         ├ open RFI (PIT)
                                                         ├ history (raise-only)
                                                         └ priority
```

| Stage | Module | Decides | Key rails |
|---|---|---|---|
| Ingest | `ingest.py`, `fields.py` | typed records; workflow/person fields → `AlertAnnex` | 8, 11, 16 |
| Source | `sources.py` | PIT filtering; read-only protocol | 1, 10 |
| Linking | `linking.py` | episodes = alerts on one trade within `linking.window_hours`; lifecycle issues | 5 |
| Verification | `verification.py` | claim → VERIFIED / CONTRADICTED / NOT_VERIFIABLE_FROM_AVAILABLE_FIELDS | 5, 7 |
| Evidence | `evidence.py` | required items per category (config) | 6 |
| Treatment | `treatment.py`, `cases.py` | BULK_CANDIDATE only if zero reasons | 9, 12 |
| Priority | `priority.py` | weighted features from config; no person fields | 11, 12 |
| Cohorts | `cohorts.py` | group by (category, verdict signature); status PROPOSED_AWAITING_SUPERVISOR_ATTESTATION | 2 |
| Reports | `report.py`, `agents/` | prose via `{{F<n>}}` placeholders; RFI drafts marked NOT SENT | 3, 4, 6, 13–15 |

## Decision outputs

`RunResult.decisions_json()` is canonical JSON (cases, cohorts, individual queue, versions). It is byte-identical with agents on or off. Prose, RFI drafts and manifests sit outside it.

## Fail-safe behaviour

Any `ConfigMissing` on a decision path adds `CONFIG_MISSING:<key>` and routes the case to individual review. A missing linking window disables cross-alert linking and flags every case. A missing cohort size puts every candidate into individual review.

## Data and model coupling (Phases 1–4)

| Concern | Module | Changed by |
|---|---|---|
| Real column names, codes, timestamp formats | `config/mapping.*.toml` → `mapping.py` | editing TOML (`docs/INTEGRATION.md`) |
| CSV / DB access | `adapters/csv_source.py`, `adapters/sql_source.py` | `--data`, `--sqlite`, `--db-factory` |
| Claim checks | `checkers/*.py` (registry) | new plugin + config (`docs/BUSINESS_CHANGES.md`) |
| Evidence items | `evidence.py` (registry) | new predicate + config |
| LLM provider (e.g. Qwen) | `adapters/openai_compat.py`, `adapters/gateways.py` | `[agents]` config |
| Outputs | `output.py`, `cli.py` | — |

Coverage: an entity with no mapping is reported in `decisions.data_unavailable`. A missing trade-event or (by default) RFI source routes every case to individual review.
