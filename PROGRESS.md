# PROGRESS

## Current phase: 0–4 done. Next: business confirmation of UNKNOWNs, then real-data mapping.

## Done
- **Phase 0:** deterministic core, agent runtime, invariant suite.
- **Phase 1:** `mapping.py`, `adapters/{mapped,csv_source,sql_source}.py`, coverage reasons (`DATA_UNAVAILABLE:*`), sample SCP-shaped data in `data/sample/` with `config/mapping.sample.toml`.
- **Phase 2:** `cli.py` (`run`, `check-mapping`, `check-config`, `init-mapping`), `output.py`.
- **Phase 3:** `adapters/{openai_compat,replay,gateways}.py`; `clean_model_text`; draft-rewrite prompt `narrator-2`.
- **Phase 4:** `checkers/` registry (PRICE_CORRECTION, QUANTITY_CORRECTION, CANCEL_REBOOK, LATE_BOOKING), evidence registry (+ECONOMICS_PRESENT), `config_check.py`.
- **Tests:** 137 green, plus ruff and mypy --strict.

## Decisions
- D1: Workflow and person fields are split into `AlertAnnex` at ingest (rails 8, 11). Contract v2 adds SCP's RULE_FLAG, AUTO_RFLG, REASON_STD_COMMENTS, REASON_CODE (workflow) and SUPERVISOR_GRP (person).
- D2: Agents see fact ids, labels and the placeholder draft, never values. Rail 15 holds by construction.
- D3: Bulk requires ≥1 VERIFIED claim, full evidence, a valid lifecycle, no open RFI, no adverse history and full data coverage.
- D4: Cohorts below `min_size` (including remainders) → individual (`COHORT_BELOW_MIN_SIZE`).
- D5: The individual queue orders unknown priority first, then priority descending, then id.
- D6: Aliases and renames go only through versioned mappings; exact match only.
- D7: The `notional` priority weight is per unit notional.
- D8: An unmapped `rfi_events` source blocks bulk by default (`data.rfi_source_required`, missing key ⇒ true). Unknown RFI status is not "no RFI" (rail 7).
- D9: Real-data coupling lives only in mapping TOML plus optional pure transform hooks. Unmapped source columns fail loudly per file (schema drift).
- D10: The LLM gateway sits in `adapters/` (it needs HTTP). The agent runtime (`agents/`) stays free of I/O. This matches SAD proposal P3.
- D11: The design doc `05-non-bau-suppression-flow.md` is not implemented, because it conflicts with rail 2.
- D12: CSV sources are read whole, then PIT-filtered before any use; SQL pushes the PIT predicate down.

## UNKNOWNs
- U1: Identifier semantics per SCP `SOURCE` (TRADE_ID / ALTERNATE_TRADE_ID / ORIGINAL_TRADE_ID / URN_REF). Linking currently uses TRADE_ID only.
- U2: The real claim taxonomy and evidence per category (config placeholders: PX01/CX01/LT01 in the sample).
- U3: The real source table for RFI open/close events.
- U4: The adverse-outcome vocabulary for history.
- U5: The entitlement model for person fields (currently the `--entitled` flag).
- U6: Whether `CREATED_AT` is a trustworthy record time for every SCP source (design notes call it a batch stamp).

## Audit
- Phase 0: invariant-auditor PASS R1–R16.
- Phases 1–4: invariant-auditor PASS (R2, R9 unchanged modules). Note: `--db-factory` safety depends on the supplied connection using a SELECT-only role.

## Next step
Map the real data drop with `docs/INTEGRATION.md`, then resolve U1–U6 with the business.
