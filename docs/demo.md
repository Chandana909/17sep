# Demo script (about 10 minutes)

```bash
python -m asas demo --db out/asas.db --report out/evaluation.md
python -m asas serve --db out/asas.db     # open http://127.0.0.1:8000
```

1. **Overview** (analyst.jane). Show cases in the window, bulk proposals, escalations, abstentions, zero agent failures and the valid audit chain; then the evaluation table (rules-only 0.40 vs ASAS 1.00 recall, 0 false-bulk).
2. **Cases → an ESCALATION_RECOMMENDED case.**
   - Three hypotheses were considered; OFF_MARKET_AMENDMENT is SUPPORTED against a named peer group of a given size, and the benign alternatives are CONTRADICTED with the facts.
   - Every claim cites evidence ids, and the score shows named components. This replaces the model + SHAP.
3. **Cases → a PROPOSED_BULK case in a cohort.** A verified benign explanation, inside the approved bulk policy, not drawn into the control sample.
4. **Cases → a late booking.** The agent **abstained** and names the missing evidence (execution-time source of record, incident reference). Absence of contradiction is not confirmation.
5. **Relationships.** Rebooks from the legacy system with no id link: the agent proposed them, the verifier checked the economics, and an analyst confirmed them. Linking recall went from 0.80 to 1.00 with no loss of precision.
6. **Challenger.** Confirmed blind spots (period-end window dressing no rule catches), missing context (R400 firing on like-for-like rebooks), alternative explanations and redundant detections.
7. **Discovery & Governance.**
   - The uncaptured-risk pattern and the recurring-benign pattern.
   - Open a candidate: replay report, counterexamples (the naive "any amendment" rule fails with false positives), shadow run, then submit by the analyst and approval by a *different* approver.
8. **Policy.** BUNDLE-0001 → 0002 → 0003, each with its parent and source candidate; one-click rollback.
9. **Audit.** Hash-chained entries for every ingest, run, link, candidate step and release.
10. Switch identity to **viewer.ann** and try to approve: 403. Agents have no tool for approval at all.
