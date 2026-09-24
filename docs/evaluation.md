# Evaluation

## Method

`python -m asas demo` generates 120 days of SCP/CAL-shaped data (seeded, reproducible). It covers eleven planted scenarios with ground truth:
- **Benign:** normal and large trades, FX fat-finger corrections, tagged and untagged cancel/rebooks, block allocations, small allocation amendments.
- **Risky:** off-market amendments, repeated rebook repricing, period-end window dressing (which no production rule catches).
- **Other:** late bookings (not verifiable from the contract) and degenerate URNs.

It then runs the full lifecycle and scores it with `services/evaluation.py`:
- **Linking:** pairwise precision and recall of episode membership vs the true lifecycle groups, before and after human-confirmed agent links.
- **Detection:** recall of risky episodes in the review window by production rules alone vs the agentic system (verified escalations, confirmed blind spots, released rules).
- **Treatment:**
  - bulk precision: the share of bulk proposals that are truly benign; the false-bulk count is the false-suppression risk
  - escalation precision and recall
  - abstention rate
- **Score-only baseline:** precision and recall at k (k = number of risky episodes) when ranking by the attention score alone. This stands in for "rank by a score, explain the score" approaches.

## Latest results (seed 7, 120 days)

| Metric | Value |
|---|---|
| Linking precision / recall, before → after | 1.00 / 0.80 → 1.00 / 1.00 |
| Detection recall: production rules | 0.40 |
| Detection recall: ASAS | **1.00** |
| Risky episodes found only by the agentic layer | 12 |
| Bulk proposals / false-bulk | 61 % of cases / **0** |
| Escalation precision / recall | 1.00 / 1.00 |
| Abstention | 9.7 % (all unverifiable late bookings) |
| Score-only ranking precision@k | 0.00 |

## How to read this, honestly

- **Synthetic data.** The data is synthetic with planted scenarios. The numbers show the mechanisms work end to end; they are not a production accuracy claim. Run the same harness on a real, curated, point-in-time labelled window before any claim.
- **The baseline.** The score-only row uses *our own* deterministic attention score on its own. It is not a reimplementation of any competing team's model. It shows what ranking without investigation, verification and evolution misses. A fair head-to-head uses the same labelled replay window for both systems and compares false suppression, coverage, analyst time and unexplained decisions.
- **What a score cannot do.** It cannot tell you *why* a case is fine. It cannot find a relationship that linking missed, and it cannot propose and safely release a new rule. Those are the capabilities the numbers above measure.

## Reproduce

```bash
python -m asas demo --db out/asas.db --report out/evaluation.md --seed 7 --days 120
pytest tests/test_e2e_scenarios.py -q
```
