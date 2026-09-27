# task01: verified anomaly analysis, data modularity and production hardening

## Goal

Make ASAS ready for real SCP/CAL data without having that data yet:

1. **Stronger agentic anomaly analysis.** Every episode is screened for verified peer, self-history and combination (multivariate) deviations. Explained deviations are separated from unexplained ones. Every case gets a transparent "how outlying" measure, a typology classification and a corroboration-based confidence. Everything is parameterised in config, with business context kept in a business-editable catalogue.
2. **Loose coupling to data.** Data capabilities are detected and declared, and features that need missing fields degrade to INSUFFICIENT instead of silently contradicting. A richer declarative mapping, multi-format readers and a `data` CLI (profile, draft mapping, check) let a weak model such as Qwen do the integration by editing TOML only.
3. **Industry hardening:**
   - fallbacks: model endpoint chain, ops safe mode, data freshness and volume gates, concurrent resumable pipeline
   - real authentication: OIDC JWT, trusted proxy, production start-up checks, security headers, rate limiting
   - externally anchored, signed audit log
   - Postgres backend with versioned migrations
   - Prometheus metrics, readiness checks and drift monitoring
   - CI, Docker and a lockfile
   - a performance benchmark and a real-LLM evaluation run on local `qwen2.5-coder:7b`
4. **UI:** only red, grey, black and white; a new anomaly panel.
5. **Documentation** a weak model can follow: architecture flow, change playbooks, ADRs, `AGENTS.md`.

## Scope

**In scope:** everything above, plus tests (unit, property, adversarial, e2e, API) and docs.

**Out of scope:**
- real-data calibration (no data yet)
- SSO login UI (identity comes from a verified token or proxy)
- S3/WORM vendor SDKs (a file sink is provided, plus a documented protocol)
- Kubernetes manifests

## Assumptions

- Python 3.11 with the global interpreter, as today. The dev-only packages `pyjwt` and `pgserver` are installed locally for tests. `cryptography`, `psycopg` 3.3 and `prometheus_client` are already present.
- Ollama serves `qwen2.5-coder:7b` on `localhost:11434` on CPU only, so the LLM evaluation uses a small, stratified sample.
- Decisions stay deterministic. The new anomaly layer is pure `engine/` code that agents call through a read-only tool (rails 2, 3 and 12).
- Existing demo metrics must not regress: linking recall 1.0, false-bulk 0, escalation precision 1.0.

## Relevant files (verified)

| File | Status | Role |
|---|---|---|
| `src/asas/engine/signals.py` | exists | `NUMERIC_SIGNALS`, `Baseline.compare`, `build_baseline`, `compute_signals` (baselines per month from episodes ending in the lookback) |
| `src/asas/engine/scoring.py` | exists | attention score components; `outliers` component uses `scoring.outlier_signals` |
| `src/asas/engine/hypotheses.py` | exists | `CATALOG` (8 defs), `evaluate`, `adjudicate` (anomalous > benign > abstain), `assess` |
| `src/asas/engine/evidence.py` | exists | views incl. `PeerComparison`, `peer_comparison`, `VIEW_TYPES`, `facts` |
| `src/asas/engine/decisions.py` | exists | `decide_case`: ANOMALOUS → ESCALATION; bulk gate |
| `src/asas/engine/challenge.py` | exists | BLIND_SPOT uses `adjudication.klass is ANOMALOUS` (reused unchanged) |
| `src/asas/engine/discovery.py` | exists | `episode_items` (bands from `discovery.bands`) → moved to a new `engine/items.py` for reuse |
| `src/asas/engine/snapshot.py` | exists | `Snapshot`, `build_snapshot` |
| `src/asas/agents/tools.py` | exists | closed registry; `_evidence_tool` wraps `EVIDENCE_FUNCTIONS` |
| `src/asas/agents/investigator.py` | exists | playbook proposes `hyp.applicable`; `_conclude` requires competing hypotheses evaluated + adjudicator agreement |
| `src/asas/agents/gateway.py` | exists | OpenAI-compatible, resilient (retries and breaker), caching |
| `src/asas/services/platform.py` | exists | pipeline (sequential loop), `build_gateway`, `wrap_gateway` |
| `src/asas/services/evaluation.py` | exists | metrics; score-only precision@k currently **0.00** (pre-existing weakness this task fixes) |
| `src/asas/data/ingest.py`, `data/contract.py` | exist | strict mapping (`columns`, `ignore`, `values`, `naive_timestamp_offset`), CSV only, first-error-fails |
| `src/asas/data/synthetic.py` | exists | scenarios with ground truth |
| `src/asas/store/db.py` | exists | SQLite only; `?` params, `INSERT OR IGNORE`, `rowid` ordering, triggers |
| `src/asas/api/app.py` | exists | header dev auth; `/api/health` |
| `src/asas/api/static/{index.html,app.js,styles.css}` | exist | blue/green/amber palette today |
| `src/asas/core/{config,security,tracing,logging}.py` | exist | reused |
| `tests/test_architecture.py` | exists | structural rails: engine imports, no literals in policy modules, frontend escaping |
| `config/asas.toml` | exists | all thresholds |

## File-by-file audit (findings that drive the plan)

- **`scoring.py`:** the attention score mixes benign magnitude (large notional, fat-finger corrections) with risk. It ranks zero risky episodes in the top k.
  - Fix: separate *raw outlyingness* from *residual (unexplained) outlyingness* after verified benign explanations.
- **`signals.Baseline.compare`:** returns only the first peer level with n ≥ min. There is no multi-level confirmation, no self-history, no stability check and no multivariate view.
- **`hypotheses.py` false contradictions when a field is absent:**
  - If PRICE is missing, OFF_MARKET_AMENDMENT is CONTRADICTED ("no priced amendment"), and so is PERIOD_END_ROUND_TRIP.
  - REBOOK_ECONOMICS_CHANGED returns CONTRADICTED when `price_diff_pct` is None.
  - These should be INSUFFICIENT when the field is unavailable (rail 5).
  - ROUTINE_EXECUTION can be SUPPORTED without NOTIONAL_USD, which bypasses the materiality control.
- **`decisions.decide_case`:** every ANOMALOUS conclusion escalates. There is no graded treatment for generic deviations.
- **`ingest.py`:**
  - It stops at the first bad row, which is poor feedback for iterative mapping by a weak model.
  - It reads CSV only; there are no timestamp formats, IANA zones, constants or derived copies.
- **`platform.run_pipeline`:**
  - Investigations run sequentially, which is slow with an LLM.
  - There is no freshness or volume gate and no kill switch.
  - Investigation `Exception` is caught per case, which is fine.
- **`gateway.py`:** a single endpoint; there is no secondary model.
- **`api/app.py`:** headers are trusted (four-eyes can be spoofed); `dev_auth=false` means everything returns 401. There are no security headers, rate limit, metrics or readiness endpoint.
- **`store/db.py`:**
  - It is SQLite-specific.
  - The audit chain can be rebuilt by anyone with write access to the file.
  - `load_bundle` orders by `rowid`, which is not portable.
- **Repo:** no CI, no Dockerfile, no lockfile, no `AGENTS.md`.

## Backend dataflow (after this task)

1. **Source files** (csv/parquet/xlsx/jsonl) → `data/readers.py` → `EntityMapping.to_contract` (columns, constants, copies, value maps, timestamp format/zone) → `parse_*`, collecting all errors in check mode → `SourceBundle` → `Store.ingest`, with the freshness and volume profile recorded.
2. **Snapshot:** `Store.load_bundle(as_of)` → `build_snapshot`:
   - linking
   - signals and baselines (multi-level, plus a shifted baseline for stability, plus combination counts)
   - `DataCapabilities`
   - `DeviationProfile` per episode
   - attention score (now including an `outlyingness` component)
   - detections
3. **Investigation:** the agent calls `get_deviation_profile`. It evaluates VERIFIED_PEER_DEVIATION (residual) alongside the typed hypotheses. `adjudicate` subtracts deviations explained by SUPPORTED benign hypotheses, using `anomaly.explains` from config. The result is a typed anomaly, a residual anomaly with unexplained signals, benign, or abstain.
4. **Decision:** `decide_case` adds the classification (typology, severity, confidence from corroboration lines) and a graded recommendation (residual escalation band). Safe-mode and data gates can force INDIVIDUAL_REVIEW.
5. **Evaluation** ranks by attention score, by raw outlyingness and by residual outlyingness, and reports precision@k for each.
6. **API and UI:** the case detail shows the deviation panel, classification and business context from `config/business_context.toml`.

## Database impact

- New tables:
  - `schema_migrations` (version, name, checksum, applied_at), replacing `schema_version`
  - `ops_state` (append-only; safe mode events are recorded as artifacts, so no new table is strictly needed)
- Source tables are read with `ORDER BY record_ts, <pk>` on both dialects.
- Postgres: identical logical schema; append-only triggers via plpgsql plus a TRUNCATE trigger; `deploy/postgres/roles.sql` revokes UPDATE/DELETE/TRUNCATE.
- Audit anchors are written to an **external** directory, not the database.

## Neo4j impact

None. The system has no Neo4j; the evidence graph is in-memory (`engine/graph.py`).

## Frontend impact

- `styles.css`: the palette is only black, white, greys and red, in both light and dark themes.
- `app.js`: the case detail adds:
  - an **Anomaly analysis** panel: outlyingness components, a per-signal peer table (level, n, percentile, robust z, verified, explained by), combination rarity, and classification with confidence
  - business context for the conclusion and signals
- The overview adds data capabilities, safe mode status and drift flags.
- Every new free-text field goes through `esc()`, enforced by the existing architecture test.

## API endpoints involved

- **Existing, extended:** `/api/cases/{id}` (deviation, classification, context) and `/api/overview` (capabilities, ops state, drift).
- **New:**
  - `/api/health/live`, `/api/health/ready`, `/metrics`
  - `/api/ops/safe-mode` (GET; POST admin, audited)
  - `/api/data/capabilities`
  - `/api/monitoring/drift`
  - `/api/context` (business context catalogue)

## Implementation plan

### Phase 1: verified anomaly analysis

1. **`engine/items.py`** (new): move `episode_items` out of `discovery.py`, which re-exports it for compatibility.
   - Verify: existing discovery tests are unchanged.
2. **`engine/signals.py`:**
   - Baselines group by `anomaly.peer_levels`, a superset that adds `book`.
   - `SignalSet` carries `shifted_baselines` (reference window shifted back by `anomaly.stability_shift_days`) and per-period `ItemIndex` combination counts (singletons, pairs and triples of informative items).
   - Verify: property test that the percentile is monotone in value, and the baseline is PIT-safe (no episode ending at or after the period start).
3. **`engine/deviation.py`** (new), with `SignalDeviation`, `PeerStat`, `JointRarity` and `DeviationProfile` in `domain/models.py`:
   - Screen: percentile ≥ `screen_percentile` or robust z ≥ `screen_robust_z`, on the configured tail.
   - Verify by requiring all of:
     - confirmation at ≥ `min_confirming_levels` peer levels with n ≥ `signals.min_peer_n`
     - stability against the shifted baseline (when it has enough n, otherwise marked `STABILITY_UNKNOWN`)
     - no excluding data-quality flag (`anomaly.quality_exclusions`)
   - Joint rarity: the rarest pair or triple of individually common items with reference support ≤ `joint_max_support_pct` and reference n ≥ `joint_min_reference`.
   - Outlyingness components (`peer_deviation`, `breadth`, `self_deviation`, `joint_rarity`) with config weights and bands.
   - `residual(profile, explained_signals)` gives the unexplained components and band.
   - Verify: unit tests for each verification reason, plus a property test that adding explained signals never raises the residual.
4. **`engine/capabilities.py`** (new): `DataCapabilities.from_bundle` (field coverage vs `data.min_field_coverage`, `data.unavailable_fields` override, entities present); `HYPOTHESIS_FIELDS`; `SIGNAL_FIELDS`; `rule_gaps(ruleset)`.
   - Snapshot gets `capabilities` and `deviations`.
   - Verify: tests with PRICE and NOTIONAL removed from the data.
5. **`engine/hypotheses.py`:**
   - `HypothesisDef` gains `fields` and `residual`.
   - `Subject` carries `unavailable`.
   - `evaluate` returns INSUFFICIENT `FIELD_UNAVAILABLE:X`.
   - New `VERIFIED_PEER_DEVIATION` (residual, always applies, requires `get_deviation_profile`).
   - `adjudicate` computes the unexplained set and ranks typed anomaly > residual (unexplained) > benign > abstain.
   - `Adjudication.unexplained` is added.
   - Verify: adjudication table tests; with a field missing, no false CONTRADICTED.
6. **`engine/evidence.py` + `agents/tools.py`:** `DeviationView` and the `get_deviation_profile` tool (read-only, bounded); the investigator's tool set is extended.
   - Verify: tool tests and the architecture test.
7. **`engine/classification.py`** (new) + `decide_case`:
   - Classification (typology, severity from `classification.severity`, confidence from corroboration lines vs `classification.confidence_*`).
   - Residual treatment by `anomaly.residual_escalation_band`.
   - Reasons `UNEXPLAINED_DEVIATION:<signals>`, `FIELD_UNAVAILABLE:*`.
   - `Case` gains `classification` and `deviation` (optional).
   - Verify: decision tests; bulk is impossible with any unexplained verified deviation.
8. **`engine/scoring.py`:** add the `outlyingness` component (raw) from the profile.
   - Verify: score tests; ordering is deterministic.
9. **`data/synthetic.py`:** new risky scenarios QTY_INFLATION (large post-trade quantity increase, price unchanged, below R400) and AMEND_CHURN (many small same-day amendments, each below R100). Neither is caught by any rule or typed hypothesis. Ground truth is updated.
   - Verify: e2e tests that both are found only by the deviation layer, and benign scenarios stay non-anomalous.
10. **`services/evaluation.py`:** precision@k for attention, raw outlyingness and residual outlyingness; anomaly-flag precision; detection includes residual anomalies.
    - Verify: the evaluation test asserts residual precision@k > attention precision@k.
11. **`config/business_context.toml` + `core/context.py`:** titles, business meaning, risk rationale and reviewer checklist per hypothesis/typology, signal, reason code and rule.
    - Verify: a completeness test (every catalogue type, signal and reason prefix has context).

### Phase 2: data modularity

12. **`data/readers.py`** (new): csv, jsonl, parquet (pyarrow, optional) and xlsx (openpyxl, optional); `reader` per entity or inferred from the extension.
13. **`ingest.py` mapping:** `constants`, `copy`, `timestamp_format`, `timezone` (IANA), `decimal_separator`; `validate_source()` collects all errors (row, column, value, hint), capped by `data.max_reported_errors`.
14. **`data/profiling.py` + CLI `asas data profile | draft-mapping | check | capabilities`:** a synonym-driven draft mapping (`config/mapping.synonyms.toml`) and a capability matrix (which hypotheses, signals and rules work with the mapped fields).
    - Verify: the CLI tests round-trip synthetic data renamed to "SCP-style" column names.

### Phase 3: fallbacks and ops controls

15. **`gateway.FallbackGateway`:** an ordered endpoint chain (`[[agents.fallbacks]]`), each with its own breaker; the recorded response model id is the one that answered.
16. **Ops safe mode:** `services/ops.py` (audited `ops_state` artifact: `bulk_suspended`, `llm_suspended`, reason, actor); the pipeline honours it. API GET/POST (admin).
17. **Data gates:** freshness (`pipeline.max_staleness_hours`) and volume (`pipeline.min_volume_ratio` vs the trailing median) → the run is marked `degraded`, with bulk suppressed (`DATA_GATE:*`).
18. **Concurrent pipeline:** `pipeline.max_workers` thread pool; snapshot, graph and memory are prebuilt; results are ordered deterministically.
    - Verify: results with workers=4 equal those with workers=1.

### Phase 4: security and audit

19. **`core/auth.py`:** `DevHeaderAuthenticator` (dev only), `TrustedProxyAuthenticator` (shared secret via `hmac.compare_digest`), `OidcAuthenticator` (PyJWT + JWKS; RS/ES algorithms only; iss/aud/exp; claim → role/desk mapping from config); `security.environment`.
20. **`core/readiness.py` + `asas doctor`:** production start-up checks (no dev auth in prod, https model endpoint when not local, anchor key present, and so on).
21. **API middleware:** security headers, request id, body size limit, per-principal token-bucket rate limit.
22. **`store/anchor.py`:** Ed25519 signer (HMAC fallback), `FileAnchorSink` (exclusive create, read-only), `verify_with_anchors`; CLI `asas audit anchor | verify`; auto-anchor after each pipeline run when configured.
    - Verify: a test that rebuilds the whole chain after tampering is caught by the anchors.

### Phase 5: storage

23. **`store/dialect.py` + `store/migrations.py`:**
    - `SqliteDialect` and `PostgresDialect` (param translation, upsert-ignore, identity, trigger DDL); versioned migrations with checksums.
    - `Store(url)` accepts a path, `sqlite:///...` or `postgresql://...`.
    - `deploy/postgres/roles.sql`.
    - Verify: the store contract test suite runs on both backends (Postgres via `pgserver`, skipped if unavailable).

### Phase 6: observability

24. **`core/metrics.py`** (prometheus_client): HTTP, pipeline, agent, tool, gateway, audit, freshness, safe mode; `/metrics`; readiness and liveness endpoints.
25. **`services/monitoring.py`:** a drift report per run vs trailing runs (bulk, escalation, abstention, fallback rates, alert volume by rule, baseline medians), with tolerances in `[monitoring]`; artifact + API + metrics; `deploy/prometheus/alerts.yml`.

### Phase 7: UI

26. The red/grey/black/white palette, the anomaly panel, classification, business context, and the ops and capabilities overview.
    - Verify: in the browser pane, plus a test that no other colour literals appear in `styles.css`.

### Phase 8: delivery and evidence

27. `.github/workflows/ci.yml` (lint, type, tests on 3.11/3.12, Postgres service, pip-audit, docker build), `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `requirements.lock` (pinned).
28. **`asas bench`:** stage timings and peak memory at 1×, 5× and 10× volume → `docs/performance.md`.
29. **`asas llm-eval`:** real-model harness (valid-action rate, repair, fallback, agreement with the playbook, verified-conclusion rate, injection behaviour, latency); run on the local `qwen2.5-coder:7b` → `docs/llm-evaluation.md`.

### Phase 9: documentation

30. `docs/architecture.md` (flow diagrams per stage), `docs/anomaly-analysis.md`, `docs/playbooks/*.md` (integrate data, add field, add signal, add hypothesis, add rule, calibrate, change business context, swap model), `docs/security.md`, `docs/deployment.md`, `docs/operations.md`, ADR 0009–0015, `AGENTS.md`, README and CLAUDE.md updates.

## Test plan

- **Unit:** deviation verification reasons, joint rarity, residual, capabilities, classification, readers, mapping transforms, auth (valid, expired, wrong aud/iss, alg none, HS256 rejected, unknown kid), anchors, dialect SQL translation, metrics exposition, drift.
- **Property (hypothesis):**
  - the residual is ≤ the raw value
  - the percentile is monotone
  - mapping validation never raises past `validate_source`
  - the fallback chain always returns or raises ModelError
- **Adversarial:**
  - PRICE removed → no false CONTRADICTED
  - spoofed headers under OIDC → 401
  - tampered and rebuilt audit chain → anchor failure
  - stale data → no bulk
  - safe mode → no bulk and playbook only
  - injection text is still rejected
- **E2E:** the demo with the new scenarios:
  - linking recall 1.0
  - false-bulk 0
  - QTY_INFLATION and AMEND_CHURN found only by the deviation layer
  - residual precision@k beats the attention score
  - benign scenarios have no unexplained deviation
- **API:** new endpoints, role checks, the rate limit, and the security headers present.
- **Storage:** the contract suite on SQLite and Postgres.
- **Frontend:** the browser pane shows the overview, the anomaly panel on an escalated case and on a benign case, and the palette check.
- **Final:** `python scripts/check.py` green; invariant-auditor PASS; demo run; `asas bench`; `asas llm-eval` on local Qwen.

## Logging and debugging notes

- `log_event` for:
  - `deviation.verified` / `deviation.rejected` (signal, reason) at DEBUG
  - `pipeline.gate` (gate, value, threshold)
  - `ops.safe_mode`
  - `gateway.fallback_endpoint`
  - `auth.rejected` (reason, never the token)
  - `audit.anchored` / `audit.anchor_mismatch`
  - `migration.applied`
- Spans: `deviation.profile`, `pipeline.investigate` (per worker), `store.migrate`.

## Tracker

| # | Step | Code | T1 | T2 | T3 | T4 | T5 | Done |
|---|---|---|---|---|---|---|---|---|
| 1 | items module | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 2 | multi-level + shifted baselines, item index | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 3 | deviation engine | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 4 | data capabilities | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 5 | hypotheses: fields, residual, adjudication | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 6 | deviation tool | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 7 | classification + decisions | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 8 | outlyingness score component | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 9 | new synthetic scenarios | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 10 | evaluation ranking metrics | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 11 | business context catalogue | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 12 | readers | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 13 | mapping transforms + collected validation | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 14 | data CLI | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 15 | fallback gateway chain | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 16 | ops safe mode | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 17 | data gates | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 18 | concurrent pipeline | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 19 | authenticators | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 20 | readiness + doctor | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 21 | API middleware | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 22 | audit anchoring | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 23 | dialects + migrations + Postgres | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 24 | metrics + health | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 25 | drift monitoring | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 26 | UI palette + panels | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 27 | CI, Docker, lock | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 28 | benchmark | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 29 | real-LLM evaluation | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| 30 | documentation | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |

T1 = unit, T2 = property/adversarial, T3 = e2e, T4 = API/UI, T5 = `scripts/check.py` green.

## Open questions and risks

- **Over-flagging by the new layer.** Mitigated by multi-level confirmation, stability, data-quality exclusions and benign explanation coverage; e2e tests assert benign scenarios stay clean.
- **Joint-rarity cost** is O(items³) per episode. It's capped by `anomaly.joint_max_order` and informative items only; `asas bench` measures it.
- **Postgres locally** depends on `pgserver` starting on Windows. If it can't, the Postgres tests are CI-only and that is stated.
- **CPU-only Qwen:** the LLM evaluation uses a small sample; the results are indicative only.
- **The Docker image** can't be built locally (no Docker); CI builds it.
- **Business context wording** is a draft for compliance to own; it has no decision effect.
