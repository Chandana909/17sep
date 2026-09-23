# Field contract: `field-contract-2` (aliases `aliases-1`)

Source of truth in code: `src/asas/fields.py`. Any other column raises `UnknownFieldError`.

## Alerts
| Field | Type | Use |
|---|---|---|
| ALERT_ID | str, unique | identity |
| ALERT_TYPE | str | category mapping |
| TRADE_ID | str | linking |
| INSTRUMENT_ID, BOOK_ID | str | context |
| ALERT_TIME | tz-aware ISO timestamp | linking window |
| RECORD_TIME | tz-aware ISO timestamp | PIT |
| EXPLANATION_TEXT | str, untrusted | evidence presence only; delimited for agents |
| ALERT_GRP_ID, RFI_FLAG, AUTO_RFI_FLAG, SIGNOFF_STD_COMMENTS, SIGNOFF_COMMENTS, WF_ACTION_NAME, MESSAGE_DESCRIPTION, MSG_TEXT, RULE_FLAG, AUTO_RFLG, REASON_STD_COMMENTS, REASON_CODE | str | **quarantined** → `AlertAnnex.workflow` (rail 8) |
| TRADER_REQUESTOR, TRADE_MODIFIER, SUPERVISOR_GPN, SUPERVISOR_GRP | str | **quarantined** → `AlertAnnex.persons`; entitled reports only (rail 11) |

## Trade events
TRADE_ID, EVENT_TYPE (`NEW`/`AMEND`/`CANCEL`), EVENT_TIME, RECORD_TIME, INSTRUMENT_ID, BOOK_ID, PRICE (decimal or blank), QUANTITY (decimal or blank), SIDE (`BUY`/`SELL` or blank), ORIGINAL_TRADE_ID (or blank).

## RFI events (rail 8 exception)
ALERT_ID, RFI_ACTION (`OPENED`/`CLOSED`), RECORD_TIME.

## Past cases (ASAS-owned supervisor decisions)
CASE_ID, CATEGORY, OUTCOME, DECIDED_AT (retrieved with `decided_at < as_of`).

## Aliases (`aliases-1`, exact match)
ALERT_TIMESTAMP→ALERT_TIME, TRD_ID→TRADE_ID, INSTR_ID→INSTRUMENT_ID.

## Parsing rules
Naive timestamps, non-finite decimals, unknown enum values, duplicate ALERT_ID and alias collisions are all rejected. Timestamps are normalised to UTC.

## Source mappings (rail 16)
Real column names are mapped to these contract fields only through a versioned mapping file (`config/mapping.*.toml`, loaded by `src/asas/mapping.py`). A mapping may rename columns, translate values, attach an offset to naive timestamps, parse a declared timestamp format, and call a pure transform hook. A column present in a source but neither mapped nor ignored fails loudly. A mapping that targets a field not listed here is rejected. The mapping version is stamped on every output.

Sample SCP names (design Appendix A) mapped in `config/mapping.sample.toml`: ALERT_TYPE_ID→ALERT_TYPE, ALERT_DATE→ALERT_TIME, CREATED_AT→RECORD_TIME, INSTRUMENT_IDENTIFIER→INSTRUMENT_ID, BOOK→BOOK_ID, REASON_COMMENT→EXPLANATION_TEXT, EVENT_SUB_TYPE_ID→EVENT_TYPE, TRADE_DATE_TIME→EVENT_TIME, BUY_OR_SELL→SIDE.
