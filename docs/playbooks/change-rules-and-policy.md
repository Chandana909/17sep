# Playbook: the business wants a rule or the bulk policy changed

**Goal:** change detection logic or bulk eligibility the only allowed way, through governance.
**You never edit:** `config/rulesets/production-v1.json` after the first seed. It only seeds `BUNDLE-0001`. Every later change is a new immutable, versioned policy bundle.

## 1. Write the rule in the DSL

A rule is a list of conditions over named signals. Signals are listed in `src/asas/engine/signals.py` (`NUMERIC_SIGNALS`, `FLAG_SIGNALS`, `LABEL_SIGNALS`); see `docs/anomaly-analysis.md` for their meaning. Operators: `gt ge lt le eq ne in`. Thresholds go in `parameters` and are referenced with `param`.

To **change** an existing rule, reuse its `rule_id`: on release it replaces that rule. To **add** a rule, use a new `rule_id`.

```json
{
  "rule_id": "R100",
  "subrule_id": "R100.1",
  "description": "Price amendment at or above the threshold",
  "severity": "MEDIUM",
  "conditions": [{"signal": "max_price_change_pct", "op": "ge", "param": "min_price_change_pct"}],
  "parameters": {"min_price_change_pct": "0.5"}
}
```

## 2. Propose it

```bash
python -m asas propose --db out/real.db --rule-file r100-v2.json --rationale "Compliance memo 2026-07: lower R100 to 0.5%"
```

A bulk-review scope works the same way: `--bulk-scope PRICE_CORRECTION@FX`. The console and the API (`POST /api/candidates`) do the same. The proposal is validated against the DSL, audited and stored as a DRAFT candidate.

## 3. Evidence chain (console: Discovery & Governance, or the API)

1. **replay:** current rules vs rules plus the candidate on curated history.
2. **counterexamples:** false positives, false negatives, boundary cases, mutations, instability.
3. **shadow:** runs on the most recent window with no effect.
4. **submit:** by the proposer, with a note.
5. **approve:** by a **different** approver (four-eyes). This releases a new bundle and records its parent.

Any failed gate stops the chain, and the reports stay on the candidate. `POST /api/policy/rollback` re-activates an earlier bundle (audited).

## Done means

- [ ] the candidate shows REPLAYED, COUNTEREXAMPLES_PASSED, SHADOW_PASSED, AWAITING_APPROVAL, RELEASED
- [ ] the new bundle appears under Policy, with the candidate as its source
- [ ] the next pipeline run's manifest names the new bundle
