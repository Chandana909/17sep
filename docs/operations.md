# Operations

## Daily run

```bash
python -m asas pipeline          # cases for the review window at the data watermark
python -m asas challenge         # blind spots (most outlying first), missing context, ...
python -m asas audit anchor      # sign the audit chain head into audit.anchor_dir
python -m asas verify-audit
```

`--db` defaults to `$ASAS_DATABASE_URL`. The pipeline anchors automatically when `audit.anchor_dir` is set.

Every run records:
- its config version and fingerprint, snapshot, policy bundle, and model / prompt / tool-schema versions (`GET /api/runs/{run_id}`)
- its data gates, safe-mode state, unavailable fields and rule coverage gaps (`GET /api/overview`)
- its KPIs and a drift report (`GET /api/monitoring/drift`)

## Configuration

`config/asas.toml` holds every threshold, tolerance, window, weight, limit and budget. Decimals are strings. A missing or malformed decision key raises `ConfigMissing`; nothing invents a default. Environments use overlays (`docs/deployment.md`).

## Failure modes

| Failure | Behaviour |
|---|---|
| Model returns invalid JSON or schema | one repair attempt, then the playbook takes the step (`fallback_used`) |
| Model repeats itself or keeps being rejected | after `agents.max_consecutive_rejections` stalled steps the playbook finishes the run |
| Model tries to abstain early or conclude unsupported | rejected; it can abstain only where the verifier would |
| Model outage | retries with backoff → circuit breaker → next endpoint in `agents.fallbacks` → playbook |
| Worker dies mid-investigation | the next run resumes from the last checkpoint |
| Agent runtime raises | that case alone goes to `INDIVIDUAL_REVIEW` with `AGENT_UNAVAILABLE:<error>` |
| Budget exhausted | abstain with `BUDGET_*` and the missing evidence |
| Missing peer baseline | score degraded (`NO_BASELINE`), which blocks bulk proposals |
| Source field missing from the data | dependent hypotheses become INSUFFICIENT (`FIELD_UNAVAILABLE:*`) and dependent rules are listed as gaps |
| Stale or truncated feed | data gate fails: the run is degraded and bulk is blocked (`DATA_GATE:*`) |
| Source row changed in place | ingestion rejects it |
| Two writers on one store | serialised (SQLite `BEGIN IMMEDIATE`, Postgres advisory lock); the audit chain stays linear |
| Audit tampering | `verify-audit`, `/api/health/ready` and anchor verification fail |
| Anchor sink unavailable | the run stands, `audit.anchor_failed` is logged, and `asas_audit_unanchored_entries` grows until it alerts |

## Runbooks

### Audit chain broken
*Alert: AsasAuditChainBroken.* Treat it as a security incident.
1. Put the system in safe mode (bulk suspended).
2. Run `python -m asas audit verify --anchors <sink>` to locate the first entry whose hash no longer matches an anchor.
3. Preserve the database and the anchor sink.
4. Restore from backup to before that point and replay ingestion from the source extracts, which is idempotent.

### Audit anchoring
*Alert: AsasAuditNotAnchored.*
1. Check that `$ASAS_AUDIT_SIGNING_KEY` is mounted and `audit.anchor_dir` is writable by the service and not by the database account.
2. Run `python -m asas audit anchor` manually and confirm a new `anchor-<seq>.json` file.

### Pipeline failed
*Alerts: AsasPipelineFailed, AsasPipelineStale.*
1. The audit log has a `PIPELINE_FAILED` entry with the error type; the JSON logs have `pipeline.failed`.
2. Fix the cause (database connectivity, disk, config) and re-run `asas pipeline`. Completed investigations replay from their record, so re-running is cheap.

### Data gate failed
*Alert: AsasDataGateFailed.*
1. The overview banner and `report.data_gates` say which gate failed: `STALE_*` means the feed stopped; `LOW_VOLUME_*` means a partial load.
2. Fix the load, ingest again (idempotent), and re-run the pipeline.
3. Until then every case is reviewed individually. Nothing is closed or bulked.

### Safe mode
*Alert: AsasSafeModeOn after 24h.*
- Show: `python -m asas ops show`
- Resume: `python -m asas ops set --bulk resume --llm resume --reason "<why it is safe now>"`

Every change is audited with the actor and the reason.

### Drift
*Alert: AsasDriftDetected.* Open `/api/monitoring/drift`: each flag names the metric, its baseline (the median of recent runs) and the change.

| Drift | Look at |
|---|---|
| Bulk rate up | a policy release, or data that lost a field (check the Data tab) |
| Escalation or unexplained rate up | a real change in behaviour, or a baseline shift after a new desk or product |
| Alert volume | an upstream rule change in SCP |
| Fallback rate | model health (see Model outage) |

Drift never changes decisions. It tells you to look.

### Model outage
*Alerts: AsasModelCircuitOpen, AsasHighFallbackRate.*
1. Agents already run on the playbook, so outcomes are unchanged and only depth and latency suffer.
2. Check the endpoint. If a new model or prompt version caused it, suspend the LLM (safe mode) and run `python -m asas llm-eval` against the candidate model before switching back (`docs/playbooks/swap-model.md`).

### API errors
*Alert: AsasApiErrors.*
1. The JSON access logs carry a `request_id` per request (returned as `X-Request-ID`).
2. Check `/api/health/ready` (storage, policy, audit), then database connectivity.

## Governance operations

- **Releases:** discovery or a human proposal → replay → counterexamples → shadow → submit → four-eyes approval (`docs/governance.md`, `docs/playbooks/change-rules-and-policy.md`).
- **Rollback:** `POST /api/policy/rollback` (audited). Bundles are never edited.
- **Curation:** analyst decisions are RAW; only an approver who is not the decider can curate them into labels.

## Periodic controls (outside the code)

- Model-risk validation of the hypothesis catalogue, thresholds and any LLM in use; re-validate on every model or prompt change using `llm-eval` and the evaluation harness.
- Compliance approval of bulk-review scopes and of `config/business_context.toml` wording.
- A penetration test before go-live, and yearly.
- Access reviews of the directory groups in `[security.role_map]`.
