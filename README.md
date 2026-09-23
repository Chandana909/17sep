# ASAS v1: supervisor bulk-review preparation

ASAS reads SCP/CAL data **read-only** at a point in time. It links alerts into lifecycle episodes and verifies operational explanations against trade data. It then proposes evidence-complete **bulk-review cohorts** and writes business-level reports. **The supervisor decides everything.** Nothing is suppressed, signed off or sent.

Python 3.11+, no runtime dependencies.

## Quick start

```bash
pip install -e ".[dev]"
python -m asas check-config
python -m asas check-mapping --data data/sample --mapping config/mapping.sample.toml --as-of 2026-01-12T00:00:00+00:00
python -m asas run --data data/sample --mapping config/mapping.sample.toml --as-of 2026-01-12T00:00:00+00:00 --out out/
```

The output goes to `out/`:

| File | Content |
|---|---|
| `decisions.json` | Canonical, byte-stable decisions: cases, proposed cohorts, individual queue, versions |
| `cases.csv` | One row per review case: treatment, cohort, queue rank, claim verdicts, reasons |
| `cohorts.csv` | Proposed bulk cohorts (`PROPOSED_AWAITING_SUPERVISOR_ATTESTATION`) |
| `reports.jsonl` | Business-level prose per case and cohort |
| `rfi_drafts.jsonl` | RFI drafts (`DRAFT_NOT_SENT`) |
| `manifests.jsonl` | One decision-environment manifest per LLM call |

## Plugging in real data

Edit a mapping file; no code changes are needed. See **[docs/INTEGRATION.md](docs/INTEGRATION.md)**.

```bash
python -m asas init-mapping --data <your_csv_dir> --out config/mapping.real.toml
```

SQL sources work through `--sqlite <file>` or `--db-factory module:function` (any DB-API driver).

## Using an LLM (optional, e.g. Qwen)

Set `[agents] enabled = true` in `config/asas.v1.toml`. Any OpenAI-compatible endpoint works: Ollama, vLLM, LM Studio or DashScope. The LLM only rewrites report prose. Its output is validated, and invalid output falls back to templates, so decisions are byte-identical with or without it.

## Business changes

Thresholds, categories, claims and evidence rules live in config. New claim types are one small plugin module. See **[docs/BUSINESS_CHANGES.md](docs/BUSINESS_CHANGES.md)**.

## Development

```bash
make check                 # or: python scripts/check.py   (ruff + mypy --strict + pytest)
```

Design: [SOLUTION.md](SOLUTION.md) · Architecture: [docs/SAD.md](docs/SAD.md) · Rails: [CLAUDE.md](CLAUDE.md) · Status: [PROGRESS.md](PROGRESS.md). The original design notes (`0*-*.md`, `prompt.*`) are kept for reference. The non-BAU suppression flow in `05-*` is intentionally **not** implemented, because rail 2 forbids suppression.
