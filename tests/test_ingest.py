"""Field contract and dirty-data handling (adversarial)."""

from __future__ import annotations

import pytest

from asas.fields import DataContractError, UnknownFieldError
from asas.ingest import parse_alerts, parse_trade_events

ALERT_ROW = {
    "ALERT_ID": "A1",
    "ALERT_TYPE": "PRICE_OFF_MARKET",
    "TRADE_ID": "T1",
    "INSTRUMENT_ID": "I1",
    "BOOK_ID": "B1",
    "ALERT_TIME": "2026-01-05T10:00:00+00:00",
    "RECORD_TIME": "2026-01-05T10:00:00+00:00",
    "EXPLANATION_TEXT": "corrected",
    "RFI_FLAG": "Y",
    "SIGNOFF_COMMENTS": "ok to close",
    "TRADER_REQUESTOR": "jdoe",
}
EVENT_ROW = {
    "TRADE_ID": "T1",
    "EVENT_TYPE": "NEW",
    "EVENT_TIME": "2026-01-05T09:00:00+01:00",
    "RECORD_TIME": "2026-01-05T09:00:00+01:00",
    "INSTRUMENT_ID": "I1",
    "BOOK_ID": "B1",
    "PRICE": "100.5",
    "QUANTITY": "",
}


def test_workflow_and_person_fields_are_quarantined() -> None:
    (alert,), (annex,) = parse_alerts([ALERT_ROW])
    assert not hasattr(alert, "rfi_flag")
    assert dict(annex.workflow) == {"RFI_FLAG": "Y", "SIGNOFF_COMMENTS": "ok to close"}
    assert dict(annex.persons) == {"TRADER_REQUESTOR": "jdoe"}


def test_unknown_column_fails_loudly() -> None:
    with pytest.raises(UnknownFieldError) as exc:
        parse_alerts([{**ALERT_ROW, "RISK_SCORE": "9"}])
    assert exc.value.fields == ["RISK_SCORE"]


def test_alias_maps_only_through_versioned_mapping() -> None:
    row = {k: v for k, v in ALERT_ROW.items() if k != "TRADE_ID"}
    (alert,), _ = parse_alerts([{**row, "TRD_ID": "T9"}])
    assert alert.trade_id == "T9"
    with pytest.raises(UnknownFieldError):
        parse_alerts([{**row, "trade_id": "T9"}])  # case variants are not aliases


def test_alias_collision_rejected() -> None:
    with pytest.raises(DataContractError):
        parse_alerts([{**ALERT_ROW, "TRD_ID": "T9"}])


@pytest.mark.parametrize(
    "patch",
    [
        {"ALERT_TIME": "2026-01-05T10:00:00"},  # naive
        {"ALERT_TIME": "yesterday"},
        {"ALERT_ID": "  "},
        {"EXPLANATION_TEXT": 42},
    ],
)
def test_dirty_alert_rows_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(DataContractError):
        parse_alerts([{**ALERT_ROW, **patch}])


def test_duplicate_alert_id_rejected() -> None:
    with pytest.raises(DataContractError):
        parse_alerts([ALERT_ROW, ALERT_ROW])


@pytest.mark.parametrize(
    "patch", [{"PRICE": "NaN"}, {"PRICE": "Infinity"}, {"PRICE": "1e"}, {"EVENT_TYPE": "DELETE"}]
)
def test_dirty_event_rows_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(DataContractError):
        parse_trade_events([{**EVENT_ROW, **patch}])


def test_event_parsing_normalises_to_utc_and_blank_to_none() -> None:
    (ev,) = parse_trade_events([EVENT_ROW])
    assert ev.event_time.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert ev.event_time.hour == 8
    assert ev.quantity is None
