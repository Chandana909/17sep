# ASAS: auditable agentic surveillance

ASAS replaces the "rules → features → ML ensemble → SHAP score" pipeline with an adaptive detection system:
- **Agents** investigate cases, challenge existing rules, discover uncaptured patterns and propose changes to the detection logic.
- **Deterministic code** decides: verified peer-deviation analysis, hypothesis verification, historical replay, evidence.
- **Human governance** approves every production change.

```
Extracts (read-only) ─► mapping + data gates ─► episodes (linking) ─► signals + frozen peer baselines
                                                                           │
          verified deviation analysis (how outlying · vs whom · real? · explained?)
                                                                           │
Agentic investigation: competing hypotheses · evidence retrieval · verifier · abstention
                                                                           │
Classification (typology · severity · confidence) ─► case decision ─► human review / attestation
                                                                           │
Curated outcomes ─► discovery / human proposals ─► replay ─► counterexamples ─► shadow ─► four-eyes release
```

## What it does

| Capability | How |
|---|---|
| Links alerts into business episodes | Deterministic two-stage linking (strong lineage, then guarded medium keys). Agent-found relationships are verified and **quarantined until a human confirms them**. |
| Measures how outlying an episode is, and whether that is real | Peer percentiles and robust z across named populations, multi-level confirmation, stability check, data-quality exclusion, rare combinations ([docs/anomaly-analysis.md](docs/anomaly-analysis.md)) |
| Separates explained from unexplained behaviour | Deviations a verified benign explanation accounts for are subtracted. **An unexplained verified deviation can never be bulk-proposed.** |
| Investigates cases dynamically | Competing hypotheses, tool calls driven by what is missing, a deterministic verifier, abstention when evidence is insufficient |
| Classifies every case | Category, typology, severity (materiality-aware) and confidence from named, independent lines of evidence |
| Challenges existing rules | Blind spots (most outlying first), missing context, alternative explanations, redundant detections |
| Evolves detection safely | Discovered or human-proposed candidates → replay → counterexamples → shadow → **four-eyes approval** → immutable versioned policy bundle, with rollback |
| Adapts to the data you have | Missing fields degrade the dependent checks to abstention; a capability matrix says exactly what works ([playbook](docs/playbooks/integrate-real-data.md)) |
| Runs safely in production | OIDC or authenticating-proxy identity, externally anchored signed audit chain, PostgreSQL with least-privilege roles, safe mode, data gates, model fallback chain, Prometheus metrics, drift monitoring |

**Deterministic boundary.** The LLM never produces numbers, dates, statistics, relationships, rule results or facts. Tools compute, verifiers decide, and agents choose what to investigate and propose. A weak, broken or hijacked model changes cost, not outcomes: this is tested with scripted and real (local Qwen) models.

## Results on realistic synthetic data (`python -m asas demo`)

| | Rules only | ASAS |
|---|---|---|
| Linking recall (precision 1.00) | 0.80 | **1.00** |
| Detection recall, risky episodes in the review window | 0.32 | **1.00** |
| Risky found only by the agentic layer, of which by verified deviation analysis alone | — | 17, of which 5 |
| Cases proposed for bulk attestation / false-bulk | — | **61 %** / **0** |
| Escalation precision / recall | — | **1.00 / 1.00** |
| Precision among raw outliers → among **unexplained** outliers | 0.50 | **1.00** |
| Precision@k: attention score alone → full verified assessment | 0.12 | **1.00** |

These are synthetic results with planted ground truth: they prove the mechanisms, not production performance. See [docs/evaluation.md](docs/evaluation.md) for the method and caveats, [docs/performance.md](docs/performance.md) for timings, and [docs/llm-evaluation.md](docs/llm-evaluation.md) for a real local-model run.

## Quick start

```bash
pip install -r requirements/dev.lock && pip install --no-deps -e .     # or: PYTHONPATH=src
python -m asas demo --db out/asas.db --report out/evaluation.md
python -m asas serve --db out/asas.db      # console at http://127.0.0.1:8000
python scripts/check.py                    # format, lint, strict mypy, tests (SQLite + PostgreSQL)
docker compose up --build                  # PostgreSQL + migrations + API (after `asas audit keygen --out secrets`)
```

## When real data arrives

Follow [docs/playbooks/integrate-real-data.md](docs/playbooks/integrate-real-data.md):

```bash
python -m asas data profile --data data/real
python -m asas data draft-mapping --data data/real --out config/mappings/scp.toml
python -m asas data check --data data/real --mapping config/mappings/scp.toml   # repeat until OK
```

No Python changes are needed for a new source. All business knobs are in `config/asas.toml`, the wording in `config/business_context.toml`, and rule changes go through `asas propose`. [AGENTS.md](AGENTS.md) and [docs/playbooks/](docs/playbooks/) are written so that a small coding model (for example Qwen in OpenCode) can make these changes safely.

## Repository map

| Path | Contents |
|---|---|
| `src/asas/core` | config (+ overlays), auth, readiness, metrics, rate limit, tracing, JSON logs, business context |
| `src/asas/data` | field contract, readers (csv/jsonl/parquet/xlsx), mapping + validation, profiling and draft mapping, synthetic generator |
| `src/asas/store` | SQLite/PostgreSQL dialects, versioned migrations, append-only store, signed audit anchors |
| `src/asas/engine` | linking, signals and baselines, **deviation analysis**, data capabilities, data gates, rule DSL, scoring, evidence, hypotheses, **classification**, decisions, graph, challenge and discovery detectors |
| `src/asas/agents` | runtime (checkpoints, budgets, recovery, fallback chain, caching, manifests), tools, prompts, Investigator, Challenger, Discovery, memory |
| `src/asas/evolution` | replay, counterexamples, shadow, governance |
| `src/asas/services` | platform, ops (safe mode), monitoring (drift), integration report, evaluation, LLM evaluation, benchmark, demo |
| `src/asas/api` | FastAPI backend and the console (red/grey/black/white, strict CSP) |
| `config/` | `asas.toml`, `business_context.toml`, `mapping.synonyms.toml`, `overlays/`, `rulesets/` |
| `deploy/` | PostgreSQL roles, Prometheus alert rules |
| `docs/` | architecture, anomaly analysis, playbooks, security, deployment, operations, ADRs, evaluation |

## Documentation

- [Architecture](docs/architecture.md), [anomaly analysis](docs/anomaly-analysis.md), [architecture decisions](docs/adr/)
- [Playbooks for business changes](docs/playbooks/README.md), [field contract](docs/field-contract.md)
- [Governance](docs/governance.md), [security](docs/security.md), [deployment](docs/deployment.md), [operations and runbooks](docs/operations.md)
- [Evaluation](docs/evaluation.md), [performance](docs/performance.md), [LLM evaluation](docs/llm-evaluation.md), [demo script](docs/demo.md)
