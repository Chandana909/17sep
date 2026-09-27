# Playbook: add a field to the data contract

**When:** a real source has information the business needs in decisions (e.g. a venue or an execution-time source of record). If a column is only informational, list it in the mapping's `ignore` instead. Workflow and person columns are already quarantined in annexes.
**Approval:** a contract change is a design change. Record it in `docs/field-contract.md` and get sign-off before merging.

**You edit, in order:**

| # | File | Change |
|---|---|---|
| 1 | `src/asas/data/contract.py` | add the name to the entity's field set (`TRADE_EVENT_FIELDS` / `ALERT_CORE_FIELDS` / ...); to `REQUIRED_FIELDS` only if every source must have it; to `DECIMAL_FIELDS` / `ENUM_VALUES` / `TIMESTAMP_FIELDS` by type; a one-line entry in `FIELD_HELP` |
| 2 | `src/asas/domain/models.py` | add the attribute to the record (`TradeEvent`, `Alert`, ...), `Optional` with a default of `None` unless required |
| 3 | `src/asas/data/ingest.py` | parse it in `trade_event_from_row` / `alert_from_row` with `parse_opt_str`, `parse_decimal` or `parse_ts` |
| 4 | `src/asas/engine/capabilities.py` | if features depend on it: add it to `_EVENT_ATTR`, and to `data.measure_fields` in `config/asas.toml` so coverage is profiled |
| 5 | `config/mapping.synonyms.toml` | the column names it usually has, so drafts map it |
| 6 | `docs/field-contract.md` | the row for the entity |
| 7 | `src/asas/data/synthetic.py` | emit it in `emit(...)` and in `write_csv`, so tests and the demo exercise it |
| 8 | tests | a parse test in `tests/test_integration.py` and a round trip through `load_source_bundle` |

Then use it in a signal (`add-signal.md`) or a hypothesis (`add-hypothesis.md`).

**Point in time:** if the new field changes over versions, it must live on the version row (`TradeEvent`). Never back-fill it from a later version.

Run `python scripts/check.py`. Stored payloads are JSON of the record, and old rows simply lack the new optional attribute, so no data migration is needed for an optional field.
