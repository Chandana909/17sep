# Making business changes

Almost every business change is a **config edit** in `config/asas.v1.toml`. After any edit:

1. Bump `version = "asas-config-N"`. It is recorded in every output and manifest.
2. Run `python -m asas check-config` and fix every `PROBLEM`.
3. Run `make check` (or `python scripts/check.py`).

| Change | Where | Code change? |
|---|---|---|
| Threshold / tolerance / window | `[verification]`, `[linking]`, `[history]` | No |
| Priority weighting | `[priority.weights]` | No |
| Cohort size limits | `[cohorts]` | No |
| New alert type → existing category | `[categories.by_alert_type]` (plus a value table entry in the mapping if the source code differs) | No |
| New category using existing claims | `[categories.by_alert_type]`, `[claims]`, `[evidence.required]` | No |
| Which evidence a category requires | `[evidence.required]` (items: `TRADE_LIFECYCLE_VALID`, `ALL_CLAIMS_VERIFIED`, `EXPLANATION_PRESENT`, `ECONOMICS_PRESENT`) | No |
| Which outcomes count as adverse history | `[history] adverse_outcomes` | No |
| Whether a missing RFI or history source blocks bulk review | `[data]` | No |
| LLM provider / model | `[agents]` | No |
| **New claim type** | one module in `src/asas/checkers/` + config | Yes, small |
| **New evidence item** | one `@register_evidence` function in `src/asas/evidence.py` + config | Yes, small |

## Adding a claim type

```python
# src/asas/checkers/my_claim.py
from collections.abc import Sequence
from asas.checkers.registry import not_verifiable, register
from asas.config import Config
from asas.models import ClaimResult, Episode, TradeEvent, Verdict

@register("MY_CLAIM")
def my_claim(claim: str, episode: Episode, all_events: Sequence[TradeEvent], cfg: Config) -> ClaimResult:
    limit = cfg.decimal("verification", "my_claim_limit")   # never a literal (rail 12)
    ...
    return not_verifiable(claim, "why it cannot be proven")  # default when unsure (rail 7)
```

Then:
- Import the module in `src/asas/checkers/__init__.py`.
- Add `MY_CLAIM` under `[claims]` for the category.
- Add `my_claim_limit` under `[verification]`.
- Add golden tests.

The static tests automatically check that the new module has no numeric literals, no workflow or person fields and no I/O.

## Things that are deliberately **not** configurable

These are the rails in `CLAUDE.md`. Changing them requires a design review, not a config edit:
- Auto-closing, signing off or suppressing anything.
- Letting history or comparable-case outcomes make a case bulk-ready.
- Using workflow or person fields in decisions.
- Letting the LLM influence any decision.
