"""Field contract (docs/field-contract.md). Only these fields exist; unknown columns fail loudly.

Workflow/outcome and person fields are accepted at the boundary but quarantined into annex
records that decision code never receives."""

from __future__ import annotations

from collections.abc import Mapping

FIELD_CONTRACT_VERSION = "field-contract-3"

WORKFLOW_FIELDS = frozenset(
    {
        "ALERT_GRP_ID",
        "RFI_FLAG",
        "AUTO_RFI_FLAG",
        "SIGNOFF_STD_COMMENTS",
        "SIGNOFF_COMMENTS",
        "WF_ACTION_NAME",
        "MESSAGE_DESCRIPTION",
        "MSG_TEXT",
        "RULE_FLAG",
        "AUTO_RFLG",
        "REASON_STD_COMMENTS",
        "REASON_CODE",
    }
)
PERSON_FIELDS = frozenset(
    {"TRADER_REQUESTOR", "TRADE_MODIFIER", "SUPERVISOR_GPN", "SUPERVISOR_GRP", "TRADER_ID"}
)

TRADE_EVENT_FIELDS = frozenset(
    {
        "TRADE_ID",
        "TRADE_VERSION",
        "EVENT_TYPE",
        "EVENT_TIME",
        "RECORD_TIME",
        "BOOK",
        "DESK",
        "INSTRUMENT_ID",
        "PRODUCT_TYPE",
        "SIDE",
        "QUANTITY",
        "PRICE",
        "CURRENCY",
        "NOTIONAL_USD",
        "ORIGINAL_TRADE_ID",
        "ALTERNATE_TRADE_ID",
        "URN_REF",
        "SOURCE",
        "TRADER_ID",
    }
)
ALERT_CORE_FIELDS = frozenset(
    {
        "ALERT_ID",
        "RULE_ID",
        "SUBRULE_ID",
        "ALERT_TIME",
        "RECORD_TIME",
        "TRADE_ID",
        "TRADE_VERSION",
        "BOOK",
        "DESK",
        "INSTRUMENT_ID",
        "EXPLANATION_TEXT",
    }
)
ALERT_FIELDS = ALERT_CORE_FIELDS | WORKFLOW_FIELDS | (PERSON_FIELDS - {"TRADER_ID"})
RFI_EVENT_FIELDS = frozenset({"ALERT_ID", "RFI_ACTION", "RECORD_TIME"})
OUTCOME_FIELDS = frozenset(
    {"OUTCOME_ID", "ALERT_ID", "OUTCOME", "LABEL_QUALITY", "DECIDED_AT", "DECIDED_BY"}
)

ENTITY_FIELDS: Mapping[str, frozenset[str]] = {
    "trade_events": TRADE_EVENT_FIELDS,
    "alerts": ALERT_FIELDS,
    "rfi_events": RFI_EVENT_FIELDS,
    "outcomes": OUTCOME_FIELDS,
}
REQUIRED_FIELDS: Mapping[str, frozenset[str]] = {
    "trade_events": frozenset(
        {
            "TRADE_ID",
            "TRADE_VERSION",
            "EVENT_TYPE",
            "EVENT_TIME",
            "RECORD_TIME",
            "BOOK",
            "DESK",
            "INSTRUMENT_ID",
            "PRODUCT_TYPE",
            "CURRENCY",
            "SOURCE",
        }
    ),
    "alerts": ALERT_CORE_FIELDS - {"EXPLANATION_TEXT", "TRADE_VERSION"},
    "rfi_events": RFI_EVENT_FIELDS,
    "outcomes": OUTCOME_FIELDS,
}
TIMESTAMP_FIELDS = frozenset({"EVENT_TIME", "RECORD_TIME", "ALERT_TIME", "DECIDED_AT"})
ENTITIES = ("trade_events", "alerts", "rfi_events", "outcomes")
