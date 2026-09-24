# Field contract (`field-contract-3`)

Source of truth in code: `src/asas/data/contract.py`. Columns outside the contract fail loudly. Workflow/outcome and person fields are accepted at the boundary but **quarantined**: decision code never receives them.

| Entity | Fields | Quarantined |
|---|---|---|
| trade_events | TRADE_ID, TRADE_VERSION, EVENT_TYPE (NEW/AMEND/CANCEL), EVENT_TIME, RECORD_TIME, BOOK, DESK, INSTRUMENT_ID, PRODUCT_TYPE, SIDE, QUANTITY, PRICE, CURRENCY, NOTIONAL_USD, ORIGINAL_TRADE_ID, ALTERNATE_TRADE_ID, URN_REF, SOURCE | TRADER_ID (person → `TradePerson`, only the entitled `get_trader_baseline` tool reads it; raise-only) |
| alerts | ALERT_ID, RULE_ID, SUBRULE_ID, ALERT_TIME, RECORD_TIME, TRADE_ID, TRADE_VERSION, BOOK, DESK, INSTRUMENT_ID, EXPLANATION_TEXT (untrusted, delimited) | ALERT_GRP_ID, RFI_FLAG, AUTO_RFI_FLAG, SIGNOFF_STD_COMMENTS, SIGNOFF_COMMENTS, WF_ACTION_NAME, MESSAGE_DESCRIPTION, MSG_TEXT, RULE_FLAG, AUTO_RFLG, REASON_STD_COMMENTS, REASON_CODE (workflow); TRADER_REQUESTOR, TRADE_MODIFIER, SUPERVISOR_GPN, SUPERVISOR_GRP (person) → `AlertAnnex` |
| rfi_events | ALERT_ID, RFI_ACTION (OPENED/CLOSED), RECORD_TIME | open-RFI status is derived point-in-time |
| outcomes | OUTCOME_ID, ALERT_ID, OUTCOME (CLEARED/ESCALATED), LABEL_QUALITY (RAW/CURATED), DECIDED_AT, DECIDED_BY | only CURATED outcomes decided before `as_of` are labels |

**Parsing rules:**
- Naive timestamps are rejected unless the mapping declares an offset; all times are normalised to UTC.
- Non-finite decimals, unknown enum values, duplicate keys and alias collisions are rejected.
- Re-ingesting an identical row is a no-op; a changed row with the same key is rejected, because corrections must be new versions.
