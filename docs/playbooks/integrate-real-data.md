# Playbook: integrate real SCP/CAL extracts

**Goal:** load real extracts into ASAS without changing Python code.
**You edit:** one new mapping file `config/mappings/<source>.toml`, and only if needed `config/mapping.synonyms.toml`.
**You never edit:** anything under `src/asas/engine/`, `src/asas/agents/`, the rails in `CLAUDE.md`, or `docs/field-contract.md` (the contract is fixed; see `add-contract-field.md` if the business really needs a new field).

Work in this exact order, running each command and reading its output before the next step.

## 1. Put the extracts in one folder

Supported formats: `.csv`, `.tsv`, `.jsonl`, `.parquet`, `.xlsx`. One file per entity:
- trade versions (required)
- alerts (required)
- RFI events (optional)
- review outcomes (optional, but needed for replay and discovery)

```bash
python -m asas data profile --data data/real
```

For every file this prints the rows and, per column, the fill rate, distinct values, inferred types and samples. Keep this output: you need the samples in step 3.

## 2. Draft the mapping

```bash
python -m asas data draft-mapping --data data/real --out config/mappings/scp.toml
```

The draft maps columns and codes using `config/mapping.synonyms.toml`:
- every guess is marked `# REVIEW`
- every gap is marked `# TODO`
- unmatched columns go to `ignore`

## 3. Review the draft line by line

Open `config/mappings/scp.toml`:

1. **Each `# TODO required ...` line:** map the source column that holds that field (see `docs/field-contract.md` for the meaning). If the source has no such column but the value is fixed, add it under `[<entity>.constants]`, e.g. `SOURCE = "SCP"`.
2. **Each `# REVIEW: fuzzy match`:** check that the column really means that field.
3. **`ignore = [...]`:** move any column that matters out of `ignore` and into `[<entity>.columns]`. Workflow and person columns (sign-off comments, supervisor) map to their own contract names; they are quarantined and never reach decisions.
4. **Code translations `[<entity>.values.<FIELD>]`:** every code must end up as one of the allowed values shown in the comment.
5. **Timestamps:**
   - if the samples look like `31/01/2026 10:00:00`, set `timestamp_format = "%d/%m/%Y %H:%M:%S"`
   - if they carry no zone, set `timezone = "Europe/London"` (DST-aware) or `naive_timestamp_offset = "+00:00"`
   - never both
6. **RECORD_TIME:** if the source has no system record time, `copy = { RECORD_TIME = "EVENT_TIME" }` is allowed but weakens point-in-time guarantees. Write down why in a comment and tell the business.
7. **Decimal commas:** `decimal_comma = true` for values like `1.234,56`.

## 4. Check until green

```bash
python -m asas data check --data data/real --mapping config/mappings/scp.toml
```

Each distinct problem is printed once, with a row count, example rows and a `FIX:` line. Apply the FIX, then run the check again. Repeat until the first line says `OK`. For machine-readable output add `--json`.

When it is OK, the command prints the **capability matrix**: which hypotheses, deviation signals, rules and features work with the fields you have. Anything `DEGRADED` or `UNAVAILABLE` abstains to individual review instead of guessing. Report these lines to the business.

## 5. Ingest and run

```bash
python -m asas ingest --db out/real.db --data data/real --mapping config/mappings/scp.toml
python -m asas pipeline --db out/real.db
python -m asas serve --db out/real.db
```

Ingestion is idempotent. A row that changed in place is rejected because corrections must arrive as new versions. The console's **Data** tab shows the same capability matrix and the latest data gates.

## 6. Hand over for calibration

Real data will need threshold calibration; see `calibrate-thresholds.md`. Do not change thresholds just to make numbers look good: every change is versioned and audited.

## Done means

- [ ] `data check` prints `OK`
- [ ] `ingest` counts match the files' row counts
- [ ] `pipeline` runs and the report has `degraded: false`, or the failed data gate is understood
- [ ] the capability matrix was shared with the business
- [ ] `python scripts/check.py` is still green (you changed no code)
