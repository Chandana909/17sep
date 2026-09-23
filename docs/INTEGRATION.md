# Integrating real SCP/CAL data

This guide is a checklist. Every step is a file edit or a command followed by a check, so a person or a small model (for example Qwen 7B) can follow it. **Never edit Python files under `src/asas/` to integrate data.** Only the mapping file changes.

## What ASAS needs

| Entity | Required? | Contract fields (right-hand side of the mapping) |
|---|---|---|
| `alerts` | yes | ALERT_ID, ALERT_TYPE, TRADE_ID, INSTRUMENT_ID, BOOK_ID, ALERT_TIME, RECORD_TIME; optional EXPLANATION_TEXT and quarantined workflow/person fields |
| `trade_events` | yes | TRADE_ID, EVENT_TYPE (NEW/AMEND/CANCEL), EVENT_TIME, RECORD_TIME, INSTRUMENT_ID, BOOK_ID; optional PRICE, QUANTITY, SIDE (BUY/SELL), ORIGINAL_TRADE_ID |
| `rfi_events` | recommended | ALERT_ID, RFI_ACTION (OPENED/CLOSED), RECORD_TIME |
| `past_cases` | optional | CASE_ID, CATEGORY, OUTCOME, DECIDED_AT |

`RECORD_TIME` must be the time the row became known to the system (for example `CREATED_AT`). It drives point-in-time correctness and is not the business event time.

Until `rfi_events` is mapped, every case goes to individual review with the reason `DATA_UNAVAILABLE:rfi_events`. This is deliberate, because an unknown RFI status is not "no RFI". To change it, set `data.rfi_source_required = false` in the config, which is a business decision.

## Steps

1. **Put the files in one folder** (CSV with a header row, UTF-8), for example `data/real/`.
2. **Draft a mapping:**
   ```bash
   python -m asas init-mapping --data data/real --out config/mapping.real.toml
   ```
3. **Edit `config/mapping.real.toml`.** Use `config/mapping.sample.toml` as the worked example.
   - Left side = your column name. Right side = the contract field from the table above.
   - Every column must either be mapped or appear in `ignore = [...]`.
   - Codes that differ from the contract go in a value table:
     ```toml
     [trade_events.values.EVENT_TYPE]
     AMD = "AMEND"
     ```
   - Alert type codes map to the names used in `config/asas.v1.toml` `[categories.by_alert_type]` through `[alerts.values.ALERT_TYPE]`.
   - Timestamps without a timezone need `naive_timestamp_offset = "+00:00"` (or the correct offset). Non-ISO formats need `timestamp_format = "%d/%m/%Y %H:%M:%S"`.
   - Change `version = "..."` whenever you edit the file.
4. **Check it:**
   ```bash
   python -m asas check-mapping --data data/real --mapping config/mapping.real.toml --as-of 2026-01-12T00:00:00+00:00
   ```
   Fix each `PROBLEM` line and repeat until you see `mapping OK`.
5. **Run it:**
   ```bash
   python -m asas run --data data/real --mapping config/mapping.real.toml --as-of <ISO time with offset> --out out/real
   ```

## Databases instead of CSV

- Set `source = "SCHEMA.TABLE"` (or a view) for each entity.
- Set `[sql] paramstyle` to match your driver (`qmark` for sqlite/pyodbc, `pyformat` for psycopg, `named` for oracledb).
- SQLite: `--sqlite path/to.db`. Anything else: write a function that returns a DB-API connection, using a **SELECT-only** account, and pass `--db-factory my_pkg.db:connect`.
- ASAS only ever issues `SELECT <mapped columns> FROM <source> WHERE <RECORD_TIME column> <= :as_of`.

## When a column needs real logic

If renaming and value tables are not enough (for example, the event type must be derived from two columns), write a **pure** function and reference it:

```toml
[trade_events]
transform = "my_pkg.hooks:derive_event_type"   # def derive_event_type(row: dict) -> dict
derived = ["EVENT_KIND"]                         # columns the hook creates
[trade_events.columns]
EVENT_KIND = "EVENT_TYPE"
```

The hook must not do I/O and must not read the workflow or person fields to produce decision fields.

## Errors you may see

| Message | Meaning / fix |
|---|---|
| `unknown fields not in contract: ['X']` | Column `X` is in the file but not in the mapping: map it or add it to `ignore`. |
| `naive timestamp rejected` | Add `naive_timestamp_offset`. |
| `EVENT_TYPE: unknown value 'AMD'` | Add a value table entry. |
| `required fields not mapped` | A required contract field has no source column. |
| `unsafe SQL identifier` | Table/column names may only contain letters, digits, `_`, `$`, `#` and one `.`. |
