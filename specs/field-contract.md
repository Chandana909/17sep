# Field contract — `field-contract-1` (aliases `aliases-1`)

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
| ALERT_GRP_ID, RFI_FLAG, AUTO_RFI_FLAG, SIGNOFF_STD_COMMENTS, SIGNOFF_COMMENTS, WF_ACTION_NAME, MESSAGE_DESCRIPTION, MSG_TEXT | str | **quarantined** → `AlertAnnex.workflow` (rail 8) |
| TRADER_REQUESTOR, TRADE_MODIFIER, SUPERVISOR_GPN | str | **quarantined** → `AlertAnnex.persons`; entitled reports only (rail 11) |

## Trade events
TRADE_ID, EVENT_TYPE (`NEW`/`AMEND`/`CANCEL`), EVENT_TIME, RECORD_TIME, INSTRUMENT_ID, BOOK_ID, PRICE (decimal or blank), QUANTITY (decimal or blank).

## RFI events (rail 8 exception)
ALERT_ID, RFI_ACTION (`OPENED`/`CLOSED`), RECORD_TIME.

## Past cases (ASAS-owned supervisor decisions)
CASE_ID, CATEGORY, OUTCOME, DECIDED_AT (retrieved with `decided_at < as_of`).

## Aliases (`aliases-1`, exact match)
ALERT_TIMESTAMP→ALERT_TIME, TRD_ID→TRADE_ID, INSTR_ID→INSTRUMENT_ID.

## Parsing rules
Naive timestamps, non-finite decimals, unknown enum values, duplicate ALERT_ID and alias collisions are all rejected. Timestamps are normalised to UTC.
