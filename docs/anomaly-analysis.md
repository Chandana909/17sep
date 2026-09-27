# Anomaly analysis: verified deviation, explained and unexplained

ASAS answers four questions for every episode, not just "how unusual is it?":

1. **How outlying is it, and compared with whom?**
2. **Is that deviation real,** or an artefact of a small peer group, a baseline wobble or bad data?
3. **Is it explained** by a verified benign reason?
4. **What is it** (typology), **how bad** (severity), and **how sure** are we (confidence), with the evidence named?

This replaces an unsupervised outlier model plus SHAP (for example an Isolation Forest score explained after the fact). Everything is deterministic, point-in-time and configured in `config/asas.toml` (`[anomaly]` and `[classification]`). Business meaning lives in `config/business_context.toml`.

## 1. Peer deviation (`engine/deviation.py`)

The behavioural signals are in `anomaly.signals`:
- the largest price change and the largest quantity change between versions
- the number of amendments
- booking latency
- the rebook price change
- the time to the first amendment
- recurrence of the pattern in the book

Size (notional) is deliberately *not* an anomaly signal: size raises severity and triggers the materiality review, but large is not suspicious in itself.

For each signal, the value is compared with frozen monthly peer populations: episodes that ended in the lookback window before the month (`signals.baseline_lookback_days`). The peer levels are desk+product, desk and global (`anomaly.peer_levels`). Each comparison reports:

| Measure | Definition |
|---|---|
| n | size of the peer population |
| percentile | mid-rank percentile (ties share the middle of their range, so a common value never looks extreme) |
| robust z | (value − median) / (1.4826 × MAD). When MAD is 0 (most peers identical, e.g. "0 amendments") it falls back to 1.2533 × mean absolute deviation, the Iglewicz–Hoaglin convention. A departure from a constant population is maximal. |

**Screen** (`anomaly.screen_mode = "all"`): a level flags the value only if the percentile is at least `screen_percentile` **and** the robust z is at least `screen_robust_z`. Requiring both avoids two failure modes:
- count-like signals where "1" among many zeros has a high z but an ordinary rank
- heavy tails where a high rank sits close to the median

## 2. Verification

A screened deviation counts as **verified** only if all of these hold:

| Check | Why | Config |
|---|---|---|
| Deviant at ≥ N peer levels, each with n ≥ `signals.min_peer_n` | not an artefact of one small group | `anomaly.min_confirming_levels` |
| Still deviant against the reference window shifted back by `stability_shift_days` | not a baseline wobble | `anomaly.stability_shift_days`, `require_stability` |
| No excluding data-quality flag for that signal (e.g. `VERSION_GAP` taints version-to-version changes) | not a data artefact | `[anomaly.quality_exclusions]` |
| The signal's source fields are present in the data | never infer from a missing field | data capabilities |

Every deviation keeps its reasons, for example `VERIFIED_AT_3_LEVELS`, `CONFIRMED_AT_1_OF_2_LEVELS` or `DATA_QUALITY:VERSION_GAP`. The **own book history** comparison is reported and adds weight, but never verifies on its own.

## 3. Rare combinations (the multivariate view)

A forest-style model finds episodes whose *combination* of facts is unusual even when each fact is common. ASAS does the same explicitly:
1. Each episode is turned into items: event signature, lifecycle flags and magnitude bands from `discovery.bands`.
2. The item index counts every single item, pair and triple across the reference window.
3. The episode's rarest combination of **individually common** items is found (each item seen at least `joint_min_item_support` times).

It is verified when it was seen at most `joint_max_support` times in *other* episodes (leave-one-out) over a reference of at least `joint_min_reference` episodes. The output names the combination, e.g. `band:max_price_change_pct:3 + flag:near_period_end, seen 0 times in 601 other episodes`.

## 4. Outlyingness: raw and residual

Outlyingness is a sum of named components with config weights:

| Component | Raw value |
|---|---|
| `peer_deviation` | for each verified signal, min(weakest confirming robust z / `z_cap`, 1), summed |
| `breadth` | number of verified signals − 1 |
| `self_history` | verified signals that also deviate from the book's own history |
| `joint_rarity` | 1 for a verified rare combination |

Bands: HIGH ≥ `band_high`, MEDIUM ≥ `band_medium`, LOW > 0, otherwise NONE.

**Residual outlyingness** is the same sum over only the deviations that **no verified benign explanation accounts for**. The business decides what each benign explanation covers in `[anomaly.explains]`: for example, a verified `PRICE_CORRECTION` explains a large price change and an extra amendment.

## 5. The residual hypothesis and adjudication (`engine/hypotheses.py`)

`VERIFIED_PEER_DEVIATION` applies to every episode. It is SUPPORTED when the profile has verified deviations, CONTRADICTED when none survive verification, and INSUFFICIENT when there is no baseline. Adjudication splits the verified deviations three ways:
- **explained:** covered by a SUPPORTED benign hypothesis
- **pending:** covered by a benign hypothesis that is INSUFFICIENT (e.g. an unconfirmed rebook, a late booking without its incident reference). The case abstains, as before.
- **unexplained:** everything else

The precedence:
1. A catalogued anomaly (typed hypothesis) is supported.
2. Otherwise, unexplained verified deviations exist: the conclusion is `VERIFIED_PEER_DEVIATION`.
3. Otherwise, a benign explanation with nothing left unresolved.
4. Otherwise, abstain.

**Safety property: a case with any unexplained verified deviation can never be proposed for bulk review.** An unexplained deviation is escalated when its residual band reaches `anomaly.residual_escalation_band`; otherwise it goes to individual review with reason `UNEXPLAINED_DEVIATION:<signals>`.

## 6. Classification (`engine/classification.py`)

| Field | How |
|---|---|
| category | TYPED_ANOMALY, UNEXPLAINED_DEVIATION, VERIFIED_BENIGN or UNRESOLVED |
| typology | the concluded hypothesis |
| severity | `classification.severity[typology]`, one level higher above `scoring.materiality_usd` |
| confidence | the number of independent, named lines of evidence vs `confidence_high` / `confidence_medium` |

Lines of evidence for an anomaly:
- the hypothesis is verified
- a peer deviation on a signal the typology concerns (`classification.typology_signals`)
- the deviation is confirmed at every peer level
- the book's own history also deviates
- a rare combination
- a production rule also fired
- adverse curated history

For a benign conclusion:
- the explanation is verified
- every competing anomaly was contradicted
- nothing is unexplained
- curated history of the pattern is cleared

Every line is named on the case, and its meaning comes from the business context catalogue.

## 7. What the agent does with it

The investigator calls `get_deviation_profile` and sees one line per deviating signal, with the peer populations named. For example:

> `max_price_change_pct=10.7273 VERIFIED [VERIFIED_AT_3_LEVELS]: p95.14 z39.10 vs desk_product:EQ|EQUITY (n=72); ...`

That helps a model choose which hypotheses to test. The verification and the explained/unexplained split stay deterministic. The challenger's blind-spot scan examines the most outlying episodes first, so novel risk that no rule catches is surfaced even without an alert.

## 8. Evidence on the synthetic data (seed 7)

Two novel risk scenarios were added that **no production rule and no catalogued typology** covers:
- **quantity inflation:** a post-trade amendment multiplies the quantity hours later, price unchanged
- **amendment churn:** 5–7 intraday re-marks, each step below the R100 threshold

| Metric | Value |
|---|---|
| Risky episodes found *only* by verified deviation analysis | 5 (every novel-risk episode in the window) |
| Benign scenarios with an unexplained verified deviation | 0 |
| Precision among episodes with any raw outlyingness (the "outlier model" view) | 0.50 |
| Precision among episodes with **unexplained** outlyingness | **1.00** |
| Precision@k, attention score alone | 0.12 |
| Precision@k, full verified assessment | 1.00 |

Explaining deviations away removes the benign outliers (late bookings pending evidence, verified price corrections), so the queue of unexplained behaviour is almost entirely real risk. These are synthetic numbers with planted answers. Re-run `python -m asas demo` on your own labelled replay window before making claims about real data.

## 9. Calibrating on real data

See `docs/playbooks/calibrate-thresholds.md`. In short:
1. Run the pipeline on a curated historical window.
2. Inspect `anomaly_ranking` in the evaluation and the benign cases with unexplained deviations.
3. Adjust `screen_*`, `min_confirming_levels` and `[anomaly.explains]`.
4. Bump the config `version` and re-run.

Every change is visible in the run manifest's config fingerprint.
