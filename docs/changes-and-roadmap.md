# ASAS: changes in the last two versions, and the road to industry grade

## Part 1: What changed

### Version 2.0 (commit `5d8c895`): the agentic rebuild

Starting point: v1 was a rules + Isolation Forest + supervised model + SHAP pipeline. v2.0 replaced it end to end.

1. **Deterministic engine.**
   - two-stage episode linking with guards against degenerate, hub and over-long keys
   - point-in-time signals and frozen monthly peer baselines
   - a named-component attention score instead of the ML ensemble
2. **Hypothesis catalogue.** Eight benign, anomalous and context hypotheses, each with evidence requirements and a deterministic verifier (SUPPORTED, CONTRADICTED or INSUFFICIENT), plus an adjudicator.
3. **Agent runtime.** Idempotent runs, per-step checkpoints and resume, budgets, schema-validated actions, one repair attempt then the playbook fallback, retries and a circuit breaker, a response cache, and run manifests.
4. **Three agents.**
   - **Investigator:** competing hypotheses, tool calls, link proposals
   - **Challenger:** blind spots, redundant detections, missing context, alternative explanations
   - **Discovery:** pattern mining, rule synthesis
5. **A closed registry of 20 read-only tools**, with entitlement checks.
6. **Governed evolution.** Replay → counterexamples → shadow → submit → four-eyes approval → immutable policy bundles with rollback.
7. **Store and audit.** An append-only SQLite store and a hash-chained audit log.
8. **API, console and CLI:** a FastAPI API, a web console and a command line.
9. **Synthetic data and evaluation.** Synthetic data with ground truth and an evaluation harness: linking recall went from 0.80 to 1.00, detection recall from 0.40 to 1.00, with 0 cases wrongly proposed for bulk.

### Version 2.1 (commits `4576443` to `7c96c83`): verified anomaly analysis and production hardening

1. **Verified deviation analysis** (`engine/deviation.py`).
   - Peer percentile and robust z at desk+product, desk and global level, plus the book's own history.
   - A deviation counts only if several peer levels agree, it is stable against an older reference window, and no data-quality issue explains it.
   - Rare combinations of individually common facts (the multivariate view).
   - Outlyingness as a sum of named components.
2. **Residual hypothesis `VERIFIED_PEER_DEVIATION`.** Deviations explained by a verified benign reason are subtracted. Unexplained deviations never go to bulk review, and they are escalated by band.
3. **Classification** on every case: category, typology, severity (higher above materiality) and confidence counted from named lines of evidence.
4. **Business context catalogue** (`config/business_context.toml`): meaning, risk theme and reviewer checks for every code. Tests enforce completeness.
5. **New synthetic risk.** Two novel scenarios (quantity inflation, amendment churn) that only deviation analysis finds. Precision among outliers rose from 0.50 to 1.00 after explanation; precision@k from 0.12 (score) to 1.00 (verified assessment).
6. **Data modularity.**
   - CSV, JSONL, Parquet and Excel readers.
   - A richer mapping: constants, copy, timestamp formats, IANA time zones, decimal commas.
   - `asas data profile | draft-mapping | check | capabilities`.
   - A capability matrix: missing fields degrade the checks that depend on them to abstention.
7. **Fallbacks.** A chain of model endpoints, audited safe mode (suspend bulk and/or the LLM), freshness and volume data gates, and a parallel pipeline worker pool.
8. **Security.**
   - OIDC (JWT verified against JWKS) and authenticating-proxy login.
   - A production start-up guard (`asas doctor`).
   - CSP and security headers, rate limiting, a body size limit.
   - Ed25519-signed external audit anchors.
9. **Storage.** A PostgreSQL dialect, versioned checksummed migrations, and least-privilege roles. The full lifecycle runs identically on Postgres 16.
10. **Observability.** Prometheus metrics, liveness and readiness probes, drift monitoring of run KPIs, and alert rules with runbooks.
11. **Console:** red, grey, black and white only; an anomaly panel, classification, business context, a Data tab and ops controls.
12. **Delivery.** CI workflow, Dockerfile, docker-compose, hash-pinned lockfiles, config overlays per environment.
13. **Tooling.** `asas llm-eval` (tests a real model inside the runtime, run on local qwen2.5-coder:7b: 6/6 outcomes matched the playbook, injection resisted), `asas bench`, `asas propose` (human rule changes through governance).
14. **Agent hardening from real-model testing.**
    - After repeated stalls the playbook finishes the run.
    - A model can only abstain where the verifier would.
    - The next-best action is shown as advice to small models.
15. **Bugs found and fixed.**
    - SQLite audit chain could fork under two processes.
    - A stale "latest run" pointer after safe mode.
    - Recurrence signals missing from the baselines.
    - Productive steps were counted as stalls.
16. **Docs.** Architecture, anomaly analysis, eight playbooks for business changes (written so that Qwen in OpenCode can follow them), security, deployment, operations, ADR 0009–0016, `AGENTS.md`.

## Part 2: Next steps to industry grade (in order)

### Step 1: Close the open audit findings (code is written, not yet committed)
The invariant audit of v2.1 flagged four rails. The fixes exist locally and have regression tests in `tests/test_audit_fixes.py`:
- **R7:** the proposer of a candidate must not be able to approve it, even when a colleague submits it.
- **R16:** LLM safe mode must apply to every agent entry point and survive restarts.
- **R15:** a RECORD_TIME copied from EVENT_TIME must make latency checks abstain; a partial deviation screen must return INSUFFICIENT; add a bulk backstop in `decide_case`.
- **R13:** pin the business-context version and agent version in the run manifest and idempotency key.

Also:
- an unknown `security.environment` must fail closed
- decisions must be stamped with the real time
- a desk-entitlement check is needed on decisions
- mappings must refuse invented labels

Finish by running `python scripts/check.py` and the audit again, then commit.

### Step 2: Prove it on real data
1. Get 6–12 months of SCP/CAL extracts with curated outcomes.
2. Follow `docs/playbooks/integrate-real-data.md` until `asas data check` passes.
3. Calibrate `config/asas.toml` (`docs/playbooks/calibrate-thresholds.md`) on a training window; freeze it; evaluate on a later hold-out window.
4. Score the competing ML system on the same hold-out window: false bulk, recall of escalations, novel-risk coverage, analyst time.

### Step 3: Prove the model
1. Serve an instruct model (Qwen2.5-14B/32B-Instruct via vLLM) with a context window of at least 16k tokens.
2. Run `asas llm-eval --per-scenario 5` on synthetic data, then on real data. Targets: valid-action rate ≥ 0.9, full agreement, injection resisted.
3. Record the model version; re-evaluate on every model or prompt change.

### Step 4: Prove the infrastructure
1. Push, and make CI green: tests on 3.11 and 3.12 with Postgres, pip-audit, Docker image build and smoke test.
2. Run `asas bench --scales 1,3,10` and a load test at real daily volume; add incremental snapshots if a run exceeds its window.
3. Deploy to UAT:
   - managed Postgres with `deploy/postgres/roles.sql`
   - OIDC through the bank's identity provider
   - a WORM or object-lock anchor sink
   - Prometheus with `deploy/prometheus/alerts.yml`
4. Backup and restore drill; a disaster-recovery test; a documented retention policy (5–7 years).

### Step 5: Integrate with the business
1. Push recommendations and cohorts into the existing case-management tool; record attestations there.
2. Schedule the daily run (Airflow or Control-M): `pipeline → challenge → audit anchor`.
3. Analyst pilot in shadow for 4–8 weeks: measure agreement, time saved and missed escalations.

### Step 6: Sign-offs (outside the code)
1. Model-risk validation of the hypothesis catalogue, thresholds and the LLM.
2. Compliance approval of the bulk-review scopes and of the business-context wording.
3. Penetration test and data-protection review (trader data, person-data entitlements).
4. Access review of the directory groups mapped in `[security.role_map]`.

### Step 7: Run it
1. Go live with bulk proposals limited to one desk; widen scope through governed releases.
2. Monthly: review drift, recalibrate by versioned config, re-run `llm-eval`, audit-verify with the anchors.
