# AGENTS.md: instructions for coding agents (OpenCode, Qwen, Claude Code, Codex, ...)

ASAS is an auditable surveillance system. Agents *propose*; deterministic code, replay, evidence and humans *decide*. Read `CLAUDE.md` (the rails) before any change. If a request conflicts with a rail, stop and say so.

## Golden rules

1. **Find the playbook first:** `docs/playbooks/README.md`. Follow it exactly. It names the files to edit and the files never to touch.
2. **Thresholds go in `config/asas.toml`**, never in code. Bump its `version` on every change.
3. **Business wording goes in `config/business_context.toml`**, never in code.
4. **Real column names go in a mapping TOML** (`config/mappings/*.toml`), never in code.
5. **Rules and bulk policy change only through governance:** `python -m asas propose ...`, then replay → counterexamples → shadow → submit → a different approver. Never edit `config/rulesets/*.json` after the first seed.
6. **Never:**
   - add a write, SQL, HTTP or file capability to `src/asas/agents/*`
   - read person or workflow fields in `src/asas/engine/*`
   - use raw outcomes as labels
   - loosen a check to make a test pass
7. **After every change:** `python scripts/check.py`. It must print `passed` with no ruff or mypy errors. Fix the cause, not the test.

## Commands

```bash
python scripts/check.py                                   # format, lint, strict types, all tests
python -m asas demo --db out/demo.db --report out/evaluation.md
python -m asas data profile --data <dir>                  # real extracts
python -m asas data draft-mapping --data <dir> --out config/mappings/x.toml
python -m asas data check --data <dir> --mapping config/mappings/x.toml
python -m asas serve --db out/demo.db                     # console on :8000
python -m asas doctor                                     # production readiness
```

If the package is not installed, prefix with `PYTHONPATH=src` (Windows PowerShell: `$env:PYTHONPATH="src"`).

## Where things are

| You need to... | Look in |
|---|---|
| map real data | `docs/playbooks/integrate-real-data.md`, `src/asas/data/ingest.py`, `config/mapping.synonyms.toml` |
| understand a decision | `src/asas/engine/decisions.py`, `engine/hypotheses.py` (`adjudicate`), `engine/classification.py` |
| understand "how outlying" | `src/asas/engine/deviation.py`, `docs/anomaly-analysis.md` |
| add a signal / hypothesis / field | `docs/playbooks/add-*.md` |
| tune numbers | `config/asas.toml`, `docs/playbooks/calibrate-thresholds.md` |
| change the model | `docs/playbooks/swap-model.md`, `[agents]` in `config/asas.toml` |
| the API | `src/asas/api/app.py` |
| the console | `src/asas/api/static/` (palette: red, grey, black and white only; no inline styles) |
| storage | `src/asas/store/` (append-only; schema changes are new migrations in `store/migrations.py`) |
| operations | `docs/operations.md`, `docs/deployment.md`, `docs/security.md` |

## Style

Python 3.11, pydantic v2, strict mypy, ruff (line length 100). Match the surrounding code: small pure functions in `engine/`, frozen models, config getters (`cfg.decimal`, `cfg.integer`, ...), named reasons. Tests go with the change: a unit test in `tests/test_engine.py` / `test_anomaly.py`, and an end-to-end check in `tests/test_e2e_scenarios.py` when behaviour changes.
