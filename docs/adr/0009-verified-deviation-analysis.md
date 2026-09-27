# ADR 0009: Verified deviation analysis instead of an unsupervised outlier model

**Status:** accepted

**Context.** An Isolation Forest (or similar) gives an outlier score for "how unusual", explained afterwards by SHAP. It cannot say whether an anomaly is real (small peer group, baseline wobble, bad data), whether a verified business reason already accounts for it, or what typology it is. It also ranks benign size (large trades, prompt fat-finger corrections) alongside misconduct.

**Decision.** `engine/deviation.py` compares each behavioural signal with named peer populations (mid-rank percentile and robust z with a MeanAD fallback). A deviation counts only after verification:
- multi-level confirmation
- stability against a shifted reference
- no data-quality taint
- the signal's fields are present

Rare combinations of individually common facts give the multivariate view, leave-one-out. Outlyingness is a sum of named components. A residual hypothesis, `VERIFIED_PEER_DEVIATION`, lets adjudication subtract deviations that SUPPORTED benign explanations cover (`anomaly.explains`). Unexplained deviations can never go bulk. Classification adds typology, severity and corroboration-counted confidence.

**Consequences.**
- Novel behaviour with no rule and no typology is found and named (quantity inflation and amendment churn on synthetic data).
- Precision among "outliers" rises from 0.50 (raw) to 1.00 (unexplained).
- Everything is configurable and auditable.
- It needs a reference window of history (`signals.baseline_lookback_days`) and calibration on real data.
