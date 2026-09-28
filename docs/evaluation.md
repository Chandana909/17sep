# Evaluation

## Method

`python -m asas demo` generates 120 days of SCP/CAL-shaped data (seeded, reproducible) with planted scenarios and ground truth:

| Group | Scenarios |
|---|---|
| Benign | normal and large trades, FX fat-finger corrections, tagged and untagged cancel/rebooks, block allocations, small allocation amendments |
| Catalogued risk | off-market amendments, repeated rebook repricing, period-end window dressing (which no production rule catches) |
| **Novel risk** (no production rule and **no catalogued typology**) | post-trade quantity inflation; intraday amendment churn below the R100 threshold. Generated from a separate random stream so the other scenarios are unchanged. |
| Other | late bookings (not verifiable from the contract) and degenerate URNs |

It then runs the full lifecycle and scores it with `services/evaluation.py`:
- **Linking:** pairwise precision and recall of episode membership vs the true lifecycle groups, before and after human-confirmed agent links.
- **Detection:** recall of risky episodes in the review window by production rules alone vs the agentic system (verified escalations, flagged anomalies, confirmed blind spots, released rules), and how many were found *only* by verified deviation analysis.
- **Treatment:**
  - bulk precision (the false-bulk count is the false-suppression risk)
  - escalation precision and recall
  - abstention rate
- **Anomaly ranking:** four queues over the same window at k = number of risky episodes:
  - the attention score alone (the "rank by a score" approach)
  - raw verified outlyingness (what an unsupervised outlier model gives)
  - residual outlyingness (deviations no verified benign explanation covers)
  - the full verified assessment

## Latest results (seed 7, 120 days)

| Metric | Value |
|---|---|
| Linking precision / recall, before → after | 1.00 / 0.80 → 1.00 / 1.00 |
| Detection recall: production rules | 0.32 |
| Detection recall: ASAS | **1.00** |
| Risky episodes found only by the agentic layer / only by deviation analysis | 17 / 5 |
| Bulk proposals / false-bulk | 61 % of cases / **0** |
| Escalation precision / recall | 1.00 / 1.00 |
| Abstention | 9.7 % (unverifiable late bookings) |
| Precision among raw outliers → among unexplained outliers | 0.50 → **1.00** |
| Precision@k: attention score / raw outlyingness / full verified assessment | 0.12 / 0.28 / **1.00** |
| Benign scenarios with an unexplained verified deviation | 0 (tested) |

The same lifecycle on PostgreSQL 16 gives identical numbers (`tests/test_storage.py`).

## How to read this, honestly

- **Synthetic data.** The data is synthetic with planted scenarios. The numbers show the mechanisms work end to end; they are not a production accuracy claim. Run the same harness on a real, curated, point-in-time labelled window before making any claim.
- **The baselines are ours.** The score-only and raw-outlyingness rows use *our own* deterministic scorer and deviation measure without explanation. They are not a reimplementation of any competing team's model. A fair head-to-head uses the same labelled replay window for both systems and compares:
  - false suppression
  - coverage of novel risk
  - analyst time
  - unexplained decisions
- **What a score cannot do.** It cannot say *why* a case is fine, separate explained from unexplained unusualness, find a relationship linking missed, or propose and safely release a new rule. Those are the capabilities measured above.
- **The model.** These runs use the deterministic playbook. `docs/llm-evaluation.md` reports a real local model driving the same investigations; outcomes are identical by construction, and cost and latency differ.

## Reproduce

```bash
python -m asas demo --db out/asas.db --report out/evaluation.md --seed 7 --days 120
python -m pytest tests/test_e2e_scenarios.py tests/test_anomaly.py -q
```
