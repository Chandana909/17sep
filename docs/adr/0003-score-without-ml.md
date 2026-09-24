# ADR 0003: Named deterministic attention score instead of an ML ensemble

**Status:** accepted

**Context.** The previous design combined a rule baseline, an Isolation Forest and a supervised model, then explained the result with SHAP. The supervised model learns from past sign-offs, which are not ground truth; SHAP explains the model rather than the case.

**Decision.** The score is a sum of named components over point-in-time signals, with peer baselines frozen per month (mid-rank percentiles, robust z). It orders work and raises attention; it can never make a case bulk-eligible. Risk *judgement* moves to verified hypotheses and to governed rule evolution.

**Consequences.** The explanation is the evidence itself. An ML scorer can still be added later as one more *raise-only* signal behind the same interface, without changing any decision rule.
