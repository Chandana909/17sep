# Playbook: add a hypothesis (a typology or a benign explanation)

**Goal:** teach the investigator a new explanation that it can test, verify and conclude.
**You edit:**
- `src/asas/engine/hypotheses.py`
- `config/asas.toml` (`[hypotheses]`, `[classification]`, `[anomaly.explains]`)
- `config/business_context.toml`
- tests

**Rules you must keep:**
- The verifier decides, from tool outputs only.
- A missing fact gives INSUFFICIENT, never SUPPORTED or CONTRADICTED.
- No thresholds in code.
- No person or workflow fields.

## Steps

1. **Pick the class.**
   - `ANOMALOUS`: misconduct typology; escalates when supported.
   - `BENIGN`: can make a case bulk-eligible, but only through a governed bulk scope.
   - `CONTEXT`: history; raises attention only.
2. **Write three functions** in `engine/hypotheses.py`, next to the existing ones:
   - `_x_applies(subject, cfg) -> bool`: cheap precondition from `subject.flag(...)` / `subject.number(...)` signals.
   - `_x_requires(subject, cfg) -> list[ToolRequest]`: the evidence it needs, e.g. `[ToolRequest.of("compare_trade_versions", trade_id=t) for t in subject.episode.trade_ids]`.
   - `_x_eval(subject, bag, cfg) -> Evaluation`: read the evidence with `need(bag, request, ev.SomeView)` and read every threshold with `_p(cfg, "name")`. Return `SUPPORTED` with `supporting` facts, `CONTRADICTED` with `contradicting` facts, or `INSUFFICIENT` with `missing` names.
3. **Register it** in `CATALOG`. The order is the adjudication precedence: typed anomalies, then the residual `VERIFIED_PEER_DEVIATION`, then benign, then context.
   ```python
   HypothesisDef("SIDE_FLIP_AFTER_EXECUTION", HypothesisClass.ANOMALOUS, "side changed after execution",
                 _flip_applies, _versions_needed, _flip_eval, fields=frozenset({"SIDE"})),
   ```
   Set `fields` to the optional contract fields it relies on, so that it abstains if the data lacks them. Bump `CATALOG_VERSION`.
4. **Thresholds** go in `[hypotheses]` in `config/asas.toml`.
5. **Classification:**
   - add `[classification.severity] SIDE_FLIP_AFTER_EXECUTION = "HIGH"`
   - for an anomaly, also `[classification.typology_signals]`: the signals whose deviation corroborates it
   - for a benign explanation, add `[anomaly.explains]`: the deviations it accounts for. Keep this narrow, because anything listed can no longer raise an unexplained deviation when the explanation is verified.
6. **Business context:** add `[hypotheses.SIDE_FLIP_AFTER_EXECUTION]` in `config/business_context.toml` with `title`, `risk_theme`, `meaning`, `why_it_matters` and `reviewer_checks`. The tests require all five.
7. **Tests:**
   - adjudication cases in `tests/test_anomaly.py`, following the table-driven pattern there
   - if you add a synthetic scenario in `data/synthetic.py`, generate it from the separate `novel` random stream so existing data does not change, and add it to `RISK_SCENARIOS` when risky
8. Run `python scripts/check.py`, then run `python -m asas demo` and check the evaluation numbers did not regress: `false_bulk` stays 0.

A benign explanation becomes bulk-eligible only through `python -m asas propose --bulk-scope X@DESK` and the governed chain (`change-rules-and-policy.md`).
