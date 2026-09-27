# Playbook: add a behavioural signal

**Goal:** a new, point-in-time measure of an episode that rules, hypotheses and deviation analysis can use.
**You edit:**
- `src/asas/engine/signals.py`
- `config/asas.toml`
- `config/business_context.toml`
- a test in `tests/test_engine.py`
- only if the signal reads an optional field: `src/asas/engine/capabilities.py`

**Rules you must keep:**
- only records with `record_time <= eval_time` (the code already filters `events`)
- no thresholds in code
- no person or workflow fields

## Steps

1. **Name and describe it.** Add an entry to `NUMERIC_SIGNALS` (a number) or `FLAG_SIGNALS` (true/false) in `engine/signals.py`:
   ```python
   "n_side_flips": "amendments that changed the trade side",
   ```
2. **Compute it** in `base_signals(...)`, inside the loop over `by_trade`. Use only `evs` / `versions`, which are already point-in-time and sorted:
   ```python
   side_flips = sum(1 for prev, cur in pairwise(versions) if prev.side and cur.side and prev.side != cur.side)
   ```
   Then set `numeric["n_side_flips"] = Decimal(total)` (or `flags[...] = bool(...)`). Leave the key out when it cannot be computed. Never write `0` for "unknown".
3. **Optional fields.** If the signal reads SIDE, QUANTITY, PRICE or NOTIONAL_USD, add it to `SIGNAL_FIELDS` in `engine/capabilities.py`. Then a deployment without that field reports the signal UNAVAILABLE instead of misreading it.
4. **Use it in deviation analysis** (optional): add the name to `anomaly.signals` in `config/asas.toml`. Add `[anomaly.tails]` if low values are the unusual ones, and `[anomaly.quality_exclusions]` if a data-quality flag makes it untrustworthy.
5. **Describe it for the business** in `config/business_context.toml`:
   ```toml
   [signals.n_side_flips]
   title = "Side changes"
   unit = "count"
   meaning = "How many amendments flipped buy/sell; a flip after execution is rarely a typo."
   ```
   `tests/test_context.py` fails until every anomaly signal has an entry.
6. **Test it:** add a unit test in `tests/test_engine.py` that builds a tiny episode with the `ev(...)` helper and asserts the value.
7. Bump `version` in `config/asas.toml`, then run `python scripts/check.py`.

Rules can now use the signal (`python -m asas propose --rule-file ...`). Discovery can band it: add `[discovery.bands] n_side_flips = ["1", "2"]`.
