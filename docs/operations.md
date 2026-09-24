# Operations

## Configuration
`config/asas.toml` holds every threshold, tolerance, window, weight, limit and budget. Decimals are strings. Missing or malformed decision keys raise `ConfigMissing`; nothing invents a default. The config version and fingerprint are stamped on every run manifest and snapshot.

## Observability
- **Logs:** JSON lines (`core/logging.py`) with `trace_id` and `span_id` when inside a span.
- **Traces:** spans for pipeline runs, snapshot builds, agent runs and steps, and each tool call. They are stored in the `spans` table and exportable as OTLP/JSON (`OtlpJsonFileExporter`) for any OpenTelemetry collector.
- **Run manifests:** agent, version, prompt id, version and digest, model id, policy mode, tool-schema version, config version and fingerprint, snapshot id, policy bundle id, `as_of` and principal. Available via `GET /api/runs/{run_id}` together with every step and tool call.

## Failure modes

| Failure | Behaviour |
|---|---|
| Model returns invalid JSON / schema | one repair attempt, then the playbook takes the step (`fallback_used`) |
| Model outage | retries with backoff; the circuit breaker opens; the playbook completes the run |
| Model obeys injected text | unsupported conclusions are rejected; after the model budget the playbook finishes |
| Worker dies mid-investigation | the next run resumes from the last checkpoint |
| Agent runtime raises | the case is decided `INDIVIDUAL_REVIEW` with `AGENT_UNAVAILABLE:<error>` |
| Budget exhausted | abstain with `BUDGET_*` and the missing evidence |
| Missing peer baseline | score degraded (`NO_BASELINE`), which blocks bulk proposals |
| Source row changed in place | ingestion rejects it |
| Audit tampering | `verify-audit` / `/api/health` report the chain broken |

## Routine commands

```bash
python -m asas pipeline --db out/asas.db              # daily run at the data watermark
python -m asas challenge --db out/asas.db
python -m asas discover --db out/asas.db
python -m asas verify-audit --db out/asas.db
python scripts/check.py                               # format, lint, strict mypy, tests
```
