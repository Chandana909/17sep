"""Field contract (specs/field-contract.md). Only these fields exist; unknown columns fail
loudly; aliases map only through the versioned mapping below."""

from __future__ import annotations

from collections.abc import Mapping

FIELD_CONTRACT_VERSION = "field-contract-1"
ALIAS_MAPPING_VERSION = "aliases-1"

# Rail 8: workflow/outcome fields. Quarantined into AlertAnnex; never reach decisions.
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
    }
)
# Rail 11: person fields. Reports only (entitlement-gated); never policy or priority.
PERSON_FIELDS = frozenset({"TRADER_REQUESTOR", "TRADE_MODIFIER", "SUPERVISOR_GPN"})

ALERT_CORE_FIELDS = frozenset(
    {
        "ALERT_ID",
        "ALERT_TYPE",
        "TRADE_ID",
        "INSTRUMENT_ID",
        "BOOK_ID",
        "ALERT_TIME",
        "RECORD_TIME",
        "EXPLANATION_TEXT",
    }
)
ALERT_FIELDS = ALERT_CORE_FIELDS | WORKFLOW_FIELDS | PERSON_FIELDS
TRADE_EVENT_FIELDS = frozenset(
    {
        "TRADE_ID",
        "EVENT_TYPE",
        "EVENT_TIME",
        "RECORD_TIME",
        "INSTRUMENT_ID",
        "BOOK_ID",
        "PRICE",
        "QUANTITY",
    }
)
RFI_EVENT_FIELDS = frozenset({"ALERT_ID", "RFI_ACTION", "RECORD_TIME"})
PAST_CASE_FIELDS = frozenset({"CASE_ID", "CATEGORY", "OUTCOME", "DECIDED_AT"})

ALIASES: Mapping[str, str] = {
    "ALERT_TIMESTAMP": "ALERT_TIME",
    "TRD_ID": "TRADE_ID",
    "INSTR_ID": "INSTRUMENT_ID",
}


class DataContractError(ValueError):
    """Input violates the field contract or is not parseable."""


class UnknownFieldError(DataContractError):
    def __init__(self, fields: list[str]) -> None:
        super().__init__(f"unknown fields not in contract: {sorted(fields)}")
        self.fields = sorted(fields)


def normalize_row(row: Mapping[str, object], allowed: frozenset[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    unknown: list[str] = []
    for raw_key, value in row.items():
        key = ALIASES.get(raw_key, raw_key)
        if key not in allowed:
            unknown.append(raw_key)
            continue
        if key in out:
            raise DataContractError(f"duplicate field after alias mapping: {key}")
        out[key] = value
    if unknown:
        raise UnknownFieldError(unknown)
    return out
