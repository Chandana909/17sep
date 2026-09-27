"""The business context catalogue describes everything ASAS can decide or show."""

from __future__ import annotations

from asas.core.config import Config
from asas.core.context import load_context
from asas.domain.models import Category, FindingKind, Recommendation
from asas.engine.classification import CORROBORATION_CODES
from asas.engine.decisions import REASON_CODES
from asas.engine.hypotheses import CATALOG
from asas.engine.rules import load_ruleset
from asas.services.demo import DemoResult, ruleset_path


def test_every_decision_element_has_business_meaning(cfg: Config) -> None:
    context = load_context()
    required = {
        "hypotheses": [h.type for h in CATALOG],
        "signals": [*cfg.strings("anomaly", "signals"), "notional_usd_max", "joint"],
        "rules": [r.rule_id for r in load_ruleset(ruleset_path(), cfg).rules]
        + list(cfg.strings("challenger", "sampling_rules")),
        "reasons": list(REASON_CODES),
        "categories": [c.value for c in Category],
        "corroboration": list(CORROBORATION_CODES),
        "recommendations": [r.value for r in Recommendation],
        "findings": [f.value for f in FindingKind],
    }
    assert context.missing(required) == []
    for h in CATALOG:
        entry = context.entry("hypotheses", h.type)
        assert entry and entry["reviewer_checks"] and entry["risk_theme"]


def test_every_reason_and_corroboration_emitted_is_registered(demo: DemoResult) -> None:
    for case in demo.platform.cases():
        for reason in case.reasons:
            assert reason.split(":", 1)[0] in REASON_CODES, reason
        assert case.classification is not None
        for line in case.classification.corroboration:
            assert line.split(":", 1)[0] in CORROBORATION_CODES, line


def test_prefixed_codes_resolve_to_their_family() -> None:
    context = load_context()
    assert context.meaning("reasons", "OVERRIDE:ABOVE_MATERIALITY")
    assert context.meaning("corroboration", "PEER_DEVIATION:n_amends")
    assert context.entry("reasons", "NOT_A_REASON") is None
