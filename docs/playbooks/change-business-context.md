# Playbook: change the business wording

**Goal:** update what reviewers read (titles, risk themes, meanings, reviewer checks) without touching decisions.
**You edit:** `config/business_context.toml` only.

1. Find the entry by kind and code:
   - `[hypotheses.OFF_MARKET_AMENDMENT]`
   - `[signals.max_price_change_pct]`
   - `[reasons.UNEXPLAINED_DEVIATION]`
   - `[rules.R100]`
   - `[categories.*]`, `[corroboration.*]`, `[recommendations.*]`, `[findings.*]`
2. Edit the text. Keep every key. Hypotheses need `title`, `risk_theme`, `meaning`, `why_it_matters` and `reviewer_checks` (a list).
3. Bump `version` (e.g. `context-2`) and record the approver in `owner`.
4. Run `python -m pytest tests/test_context.py`. It fails if any decision element lost its description.
5. Restart the service. The console reads `/api/context` on load, and agents receive the risk themes as prompt context.

This file never changes a decision. To change a decision, see `calibrate-thresholds.md` or `change-rules-and-policy.md`.
