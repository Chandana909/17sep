# ASAS: auditable agentic surveillance

ASAS replaces the "rules → features → ML ensemble → SHAP score" pipeline with an adaptive
detection system. Agents investigate cases, challenge existing rules, discover uncaptured
patterns and propose changes to the detection logic. Deterministic computation, historical
replay, evidence and human governance remain the source of truth.

```
Events / alerts ─► Context + episode engine ─► Evidence graph ─► Agentic investigation
   (read-only)      (deterministic linking)                     hypotheses · dynamic evidence
                                                                 retrieval · abstention
                                                                        │
     Human review ◄─ Case / decision state ◄─ Deterministic verification ◄┘
          │
          ▼
  Outcomes (curated) ─► Pattern discovery ─► Candidate rule / policy ─► Historical replay
                                                                             │
 Governed production evolution ◄─ Human approval ◄─ Shadow ◄─ Counterexample attack
```

## What it does

| Capability | How |
|---|---|
| Links alerts into business episodes | Deterministic two-stage linking (strong lineage, then guarded medium keys) with degenerate-key, fan-out, span and instrument guards |
| Finds relationships linking missed | The Investigation Agent proposes links. They are verified deterministically and **quarantined until a human confirms them** |
| Investigates cases dynamically | Competing hypotheses, tool choice driven by what is missing, a deterministic verifier, abstention when evidence is insufficient |
| Challenges existing rules | The Detection Challenger confirms redundant detections, alternative explanations, missing context and blind spots |
| Discovers patterns | Itemset mining over lifecycle signals, outcomes, assessments and rule coverage (uncaptured risk / recurring benign) |
| Evolves detection safely | Candidate → replay → counterexamples → shadow → submit → **four-eyes approval** → immutable versioned policy bundle |
| Explains everything | Evidence-cited narratives, named score components, hypothesis status with supporting and contradicting facts, full tool trace |

**Deterministic boundary.** The LLM never produces numbers, dates, statistics, relationships, rule results or facts. Tools compute; verifiers decide; agents choose what to investigate and propose. Agents hold no write capability. Production change requires human approval.

## Results on realistic synthetic data (`python -m asas demo`)

| | Rules only | ASAS |
|---|---|---|
| Linking recall (precision 1.00) | 0.80 | **1.00** |
| Detection recall, risky episodes in review window | 0.40 | **1.00** |
| Cases proposed for bulk attestation | — | **61 %**, false-bulk **0** |
| Escalation precision / recall | — | **1.00 / 1.00** |
| Abstention (unverifiable late bookings) | — | 9.7 % |
| Score-only ranking (precision@k, our own scorer used alone) | 0.00 | — |

These are synthetic results with planted ground truth: they prove the mechanisms, not production performance. See [docs/evaluation.md](docs/evaluation.md) for the method, the caveats and how to run the same harness on real labelled data.

## Quick start

```bash
pip install -e ".[dev]"            # or: set PYTHONPATH=src
python -m asas demo --db out/asas.db --report out/evaluation.md
python -m asas serve --db out/asas.db      # console at http://127.0.0.1:8000
python scripts/check.py                    # format, lint, strict mypy, tests
```

The console lets you pick an identity (analyst, approver, admin, viewer); roles are enforced server-side.

## Repository map

| Path | Contents |
|---|---|
| `src/asas/core` | versioned config, ids, JSON logging, OTel-compatible tracing, principals/entitlements |
| `src/asas/data` | field contract, versioned column mapping, strict ingestion, synthetic scenario generator |
| `src/asas/store` | append-only SQLite store: immutability triggers, read-only connections, hash-chained audit |
| `src/asas/engine` | deterministic core: linking, signals and baselines, rule DSL, scoring, evidence, hypotheses, decisions, graph, challenge and discovery detectors |
| `src/asas/agents` | runtime (checkpoints, budgets, retries, circuit breaker, caching, manifests), tools, prompts, Investigator, Challenger, Discovery, memory |
| `src/asas/evolution` | replay, counterexamples, shadow, governance |
| `src/asas/services` | platform orchestration, evaluation harness, end-to-end demo |
| `src/asas/api` | FastAPI backend and the web console |
| `config/` | `asas.toml` (every threshold), `rulesets/production-v1.json`, `mapping.example.toml` |
| `docs/` | architecture, ADRs, governance, integration, evaluation, demo script, operations |
| `docs/archive/` | the original design documents this system implements and extends |

## Documentation

- [Architecture](docs/architecture.md), [architecture decisions](docs/adr/)
- [Governance and evolution](docs/governance.md)
- [Integrating real SCP/CAL data and an LLM (e.g. Qwen)](docs/integration.md)
- [Field contract](docs/field-contract.md)
- [Evaluation](docs/evaluation.md), [demo script](docs/demo.md), [operations](docs/operations.md)
