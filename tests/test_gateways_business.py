"""Phase 3 (model gateways) and Phase 4 (business extensibility)."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from asas import run
from asas.adapters.gateways import build_gateway
from asas.adapters.openai_compat import OpenAICompatibleGateway
from asas.adapters.replay import RecordingGateway, ReplayGateway
from asas.agents.gateway import FakeModelGateway, ModelRequest
from asas.agents.validation import clean_model_text
from asas.checkers import CHECKERS, register
from asas.config import Config
from asas.config_check import validate_config
from asas.models import ClaimResult, Episode, TradeEvent, Treatment, Verdict
from factories import AS_OF, alert, cfg, event, source, two_corrected_trades

GOOD = "Case {{F1}} is summarised for the supervisor."


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def test_openai_compatible_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def opener(req, timeout):  # type: ignore[no-untyped-def]
        seen["url"], seen["body"], seen["auth"] = req.full_url, json.loads(req.data), req.headers
        body = {"model": "qwen2.5", "choices": [{"message": {"content": "<think>x 1</think>ok"}}]}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setenv("K", "secret")
    gw = OpenAICompatibleGateway("http://localhost:11434/v1/", "qwen2.5", "K", opener=opener)
    resp = gw.complete(ModelRequest("t", "m", "p", "sys", "payload", ()))
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["body"]["temperature"] == 0 and seen["body"]["messages"][0]["content"] == "sys"
    assert seen["auth"]["Authorization"] == "Bearer secret"
    assert clean_model_text(resp.text) == "ok"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("<think>reasoning 42</think>\nCase {{F1}}.", "Case {{F1}}."),
        ("```\nCase {{F1}}.\n```", "Case {{F1}}."),
        ('"Case {{F1}}."', "Case {{F1}}."),
    ],
)
def test_weak_model_output_is_cleaned(raw: str, clean: str) -> None:
    assert clean_model_text(raw) == clean


def test_qwen_style_thinking_output_passes_validation_after_cleaning() -> None:
    enabled = cfg().with_value(True, "agents", "enabled")
    gw = FakeModelGateway(
        {
            t: "<think>the price is 101</think>" + GOOD
            for t in ("case_summary", "cohort_summary", "rfi_draft")
        }
    )
    result = run(two_corrected_trades(), enabled, AS_OF, gateway=gw)
    assert result.manifests and not any(m.fallback_used for m in result.manifests)


def test_record_then_replay_is_deterministic(tmp_path: Path) -> None:
    enabled = cfg().with_value(True, "agents", "enabled")
    fake = FakeModelGateway({t: GOOD for t in ("case_summary", "cohort_summary", "rfi_draft")})
    recorder = RecordingGateway(fake)
    first = run(two_corrected_trades(), enabled, AS_OF, gateway=recorder)
    recorder.save(tmp_path / "r.json")
    replayed = run(
        two_corrected_trades(), enabled, AS_OF, gateway=ReplayGateway.from_file(tmp_path / "r.json")
    )
    assert first.case_reports == replayed.case_reports
    assert first.decisions_json() == run(two_corrected_trades(), cfg(), AS_OF).decisions_json()


def test_build_gateway_respects_config() -> None:
    assert build_gateway(cfg()) is None  # disabled by default
    on = cfg().with_value(True, "agents", "enabled")
    assert isinstance(build_gateway(on), OpenAICompatibleGateway)
    assert build_gateway(on.without("agents", "base_url")) is None  # misconfigured => templates


def test_shipped_config_is_valid() -> None:
    assert validate_config(cfg()) == []


def test_config_check_catches_business_typos() -> None:
    bad = cfg().with_value(["PRICE_CORECTION"], "claims", "PRICE_CORRECTION")
    bad = bad.with_value(
        ["TRADE_LIFECYCLE_VALID", "SIGNED_OFF"], "evidence", "required", "PRICE_CORRECTION"
    )
    problems = validate_config(bad)
    assert any("PRICE_CORECTION" in p for p in problems)
    assert any("SIGNED_OFF" in p for p in problems)


def test_quantity_correction_category() -> None:
    alerts = [alert(f"A{i}", trade=f"T{i}", atype="QUANTITY_OFF") for i in range(2)]
    events = [
        e
        for i in range(2)
        for e in (event(f"T{i}", "NEW", 0, qty="100"), event(f"T{i}", "AMEND", 1, qty="105"))
    ]
    result = run(source(alerts, events), cfg(), AS_OF)
    assert all(c.treatment is Treatment.BULK_CANDIDATE for c in result.cases)


def test_new_claim_type_is_config_plus_one_function() -> None:
    """A business-added claim type needs no change to core modules."""
    name = "TEST_ALWAYS_VERIFIED"

    @register(name)
    def _check(claim: str, _e: Episode, _a: Sequence[TradeEvent], _c: Config) -> ClaimResult:
        return ClaimResult(claim, Verdict.VERIFIED, "test checker")

    try:
        business = (
            cfg()
            .with_value("NEW_CATEGORY", "categories", "by_alert_type", "NEW_ALERT")
            .with_value([name], "claims", "NEW_CATEGORY")
            .with_value(["ALL_CLAIMS_VERIFIED"], "evidence", "required", "NEW_CATEGORY")
        )
        alerts = [alert(f"A{i}", trade=f"T{i}", atype="NEW_ALERT") for i in range(2)]
        events = [event(f"T{i}", "NEW", 0) for i in range(2)]
        result = run(source(alerts, events), business, AS_OF)
        assert [c.category for c in result.cases] == ["NEW_CATEGORY", "NEW_CATEGORY"]
        assert len(result.cohorts) == 1
        assert validate_config(business) == []
    finally:
        CHECKERS.pop(name)


@pytest.mark.parametrize(
    ("rebook", "verdict"),
    [
        (event("T2", "NEW", 2, side="SELL"), Verdict.NOT_VERIFIABLE),  # side differs
        (event("T2", "NEW", 2, side="BUY", original="T9"), Verdict.NOT_VERIFIABLE),  # other trade
        (event("T2", "NEW", 2, side="BUY", original="T1"), Verdict.VERIFIED),
    ],
)
def test_rebook_uses_side_and_original_trade_id(rebook: TradeEvent, verdict: Verdict) -> None:
    alerts = [alert("A1", atype="CANCEL_AMEND_PATTERN")]
    events = [event("T1", "NEW", 0, side="BUY"), event("T1", "CANCEL", 1), rebook]
    assert run(source(alerts, events), cfg(), AS_OF).cases[0].claims[0].verdict is verdict
