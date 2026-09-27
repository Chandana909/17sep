# Playbook: calibrate thresholds on real data

**Goal:** tune `config/asas.toml` so outcomes match curated history, without overfitting.
**You edit:** `config/asas.toml` values only. Never rename or delete keys: code reads them, and a missing key fails safe (the case goes to individual review) but loudly.
**You must:** bump `version` at the top of `config/asas.toml` on every change. It is stamped on every run manifest.

## 1. Build a labelled replay window

You need `outcomes` with `LABEL_QUALITY = CURATED` covering at least 3 months. Raw sign-offs are never ground truth.

```bash
python -m asas pipeline --db out/real.db --as-of 2026-03-31T23:59:59+00:00
python -m asas challenge --db out/real.db --as-of 2026-03-31T23:59:59+00:00
```

## 2. Read what to look at

In the console (Cases, Challenger, Data), or through `/api/cases`:

| Symptom | Likely knob | Section |
|---|---|---|
| Benign cases with **unexplained** deviations (false alarms) | `screen_percentile`, `screen_robust_z`, `min_confirming_levels`; or a benign explanation missing from `[anomaly.explains]` | `[anomaly]` |
| Known misconduct not flagged | lower `screen_*`, or a signal missing from `anomaly.signals` | `[anomaly]` |
| Too many abstentions on corrections | correction tolerances | `[hypotheses]` (`price_correction_max_pct`, `correction_window_hours`, `rebook_*`) |
| Off-market typology never fires | `off_market_min_pct`, `off_market_percentile`, `off_market_robust_z` | `[hypotheses]` |
| Peer groups too small (`CONFIRMED_AT_0_OF_2_LEVELS`) | `signals.min_peer_n`, `signals.baseline_lookback_days` | `[signals]` |
| Unrelated trades linked, or rebooks not linked | `max_span_hours`, `medium_max_gap_hours`, `max_key_fanout`, `rebook_window_hours` | `[linking]` |
| Bulk share too high or low | the bulk policy scopes (governed; never edit by hand), `control_sample_rate` | `[decisions]`, `[policy]` |

## 3. Change one thing, re-run, compare

1. Change one knob.
2. Bump `version` (e.g. `asas-config-2.1.1`).
3. Re-run the same window.
4. Compare the evaluation numbers, false bulk above all (it must stay 0). Use `python -m asas demo` on synthetic data to check that nothing regressed.

## 4. Never

- Loosen a benign hypothesis so more cases go bulk because the queue is long. Bulk policy changes go through discovery → replay → counterexamples → shadow → four-eyes approval (`docs/governance.md`).
- Put a threshold into code (`tests/test_architecture.py` fails).
- Use RAW outcomes as labels.

## Done means

- [ ] `false_bulk` is 0 on the labelled window
- [ ] escalation precision and recall are recorded before and after
- [ ] the config `version` was bumped and the change is described in the commit
- [ ] `python scripts/check.py` is green
