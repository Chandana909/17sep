# Demo script (about 12 minutes)

```bash
python -m asas demo --db out/asas.db --report out/evaluation.md
python -m asas serve --db out/asas.db     # open http://127.0.0.1:8000
```

1. **Overview** (analyst.jane).
   - Cases, escalations (in red), bulk proposals (never closed automatically), abstentions and the valid audit chain.
   - The evaluation tables: rules-only 0.32 vs ASAS 1.00 recall, 0 false-bulk, and the anomaly ranking: raw outliers 0.50 precision vs **unexplained** outliers 1.00.
2. **Cases → an ESCALATION_RECOMMENDED off-market case.**
   - **Classification:** TYPED_ANOMALY, severity HIGH, confidence HIGH, with six named lines of evidence. Each one is explained in business terms.
   - **Business context:** risk theme, why it matters, reviewer checks.
   - **Anomaly analysis:**
     - the price change is an outlier versus desk+product (n=72, p95, z 39), desk and all desks
     - it is also unusual for the book itself, and stable
     - the red bar is the part that no benign explanation accounts for
   - **Hypotheses:** OFF_MARKET_AMENDMENT is SUPPORTED; PRICE_CORRECTION is CONTRADICTED with the facts. This replaces the model score + SHAP.
3. **Cases → a PROPOSED_BULK case in a cohort.** A verified benign explanation, nothing unexplained, inside the approved bulk policy, not drawn into the control sample.
4. **Cases → a late booking.** The latency is a verified outlier but *pending*: the operational explanation needs evidence the data does not have (execution-time source of record, incident reference). The agent **abstains**. Absence of contradiction is not confirmation.
5. **Challenger.**
   - Confirmed blind spots, most outlying first: period-end window dressing, and the **novel** quantity-inflation and amendment-churn episodes that no rule and no typology covers. Only verified deviation analysis found them.
   - Also: missing context (R400 on like-for-like rebooks) and redundant detections.
6. **Relationships.** Rebooks from the legacy system with no id link: proposed by the agent, economics verified, confirmed by an analyst. Linking recall went from 0.80 to 1.00.
7. **Discovery & Governance.**
   - The uncaptured-risk and recurring-benign patterns.
   - A candidate's replay, counterexamples and shadow run; submitted by the analyst and approved by a *different* approver.
   - Business changes use the same path: `asas propose --rule-file ...`.
8. **Policy.** BUNDLE-0001 → 0002 → 0003, each with its parent and source candidate; one-click rollback.
9. **Data.** The capability matrix: which hypotheses, signals, rules and features work with the fields present. Mention `asas data draft-mapping / check` for real extracts.
10. **Overview as ops.admin.** Safe mode: suspend bulk with a reason. The next run proposes nothing for bulk, escalations are unchanged, and the change is in the audit log.
11. **Audit.** Hash-chained entries for every ingest, run, link, candidate step, release and ops change. Externally anchored in production (`asas audit anchor`).
12. Switch identity to **viewer.ann** and try to approve: 403. Agents have no tool for approval at all. In production the identity comes from OIDC, not this switcher.
