"""Static validation of a decision config. Advisory: the runtime still fails safe per case,
but this catches business edits that would silently route everything to individual review."""

from __future__ import annotations

from asas.checkers import CHECKERS
from asas.config import Config, ConfigMissing
from asas.evidence import EVIDENCE_ITEMS

_DECIMALS = (
    ("history", "adverse_rate_threshold"),
    ("verification", "rebook_quantity_rel_tolerance"),
)
_INTEGERS = (
    ("linking", "window_hours"),
    ("verification", "rebook_window_hours"),
    ("history", "lookback_days"),
    ("history", "min_comparable"),
    ("cohorts", "min_size"),
    ("cohorts", "max_size"),
)
_TOLERANCE_KEYS = {
    "PRICE_CORRECTION": "price_correction_max_rel_change",
    "QUANTITY_CORRECTION": "quantity_correction_max_rel_change",
}
PROVIDERS = {"none", "openai_compatible", "replay"}


def _try(problems: list[str], fn: object, *path: str) -> object:
    try:
        return fn(*path)  # type: ignore[operator]
    except ConfigMissing:
        problems.append(f"missing or invalid: {'.'.join(path)}")
        return None


def validate_config(cfg: Config) -> list[str]:
    problems: list[str] = []
    _try(problems, cfg.require, "version")
    for path in _DECIMALS:
        _try(problems, cfg.decimal, *path)
    for path in _INTEGERS:
        _try(problems, cfg.integer, *path)
    _try(problems, cfg.strings, "history", "adverse_outcomes")
    for weight in ("alert_count", "notional", "contradicted_claims", "not_verifiable_claims"):
        _try(problems, cfg.decimal, "priority", "weights", weight)
    categories = _try(problems, cfg.require, "categories", "by_alert_type")
    for category in (
        sorted(set((categories or {}).values())) if isinstance(categories, dict) else []
    ):
        claims = _try(problems, cfg.strings, "claims", category) or ()
        for claim in claims:  # type: ignore[attr-defined]
            if claim not in CHECKERS:
                problems.append(f"claims.{category}: no checker registered for {claim}")
            if claim in _TOLERANCE_KEYS:
                _try(problems, cfg.decimal, "verification", _TOLERANCE_KEYS[claim])
        items = _try(problems, cfg.strings, "evidence", "required", category) or ()
        for item in items:  # type: ignore[attr-defined]
            if item not in EVIDENCE_ITEMS:
                problems.append(f"evidence.required.{category}: unknown item {item}")
    try:
        if cfg.integer("cohorts", "min_size") < 1 or cfg.integer(
            "cohorts", "max_size"
        ) < cfg.integer("cohorts", "min_size"):
            problems.append("cohorts: need 1 <= min_size <= max_size")
    except ConfigMissing:
        pass
    agents = cfg.data.get("agents", {})
    if agents.get("enabled") is True and agents.get("provider", "none") not in PROVIDERS - {"none"}:
        problems.append(f"agents.provider must be one of {sorted(PROVIDERS - {'none'})}")
    return problems
