# PROGRESS

## Current phase: 0 — done. Next: Phase 1 (awaiting approval)

## Done (Phase 0)
- Repo scaffold: `pyproject.toml`, `Makefile`, `scripts/check.py`, `config/asas.v1.toml`.
- Core: `fields`, `models`, `ingest`, `sources`, `linking`, `verification`, `evidence`, `treatment`, `priority`, `cases`, `cohorts`, `report`, `serialize`, `pipeline`.
- Agents: `gateway` (ModelGateway + FakeModelGateway), `validation` (rail 4), `freetext` (rail 6), `tools` (closed registry), `manifest` (§7), `narrator`.
- Tests (82, green): golden archetypes, unit rails, adversarial ingest and injection, hypothesis properties (order-determinism, idempotence, future-record invariance, no dropped case), static AST invariants (no workflow/person fields or numeric literals in decision modules, no I/O in the agent runtime, no mutating source API, no business-action functions).

## Decisions
- D1: Workflow and person fields are split into `AlertAnnex` at ingest. Decision models physically lack them (rails 8, 11).
- D2: Agents receive fact ids and labels, never values. Agent output affects only prose and RFI drafts, so rail 15 holds by construction.
- D3: Bulk requires at least one VERIFIED claim, full evidence, a valid lifecycle, no open RFI and no adverse history. Any reason at all → individual.
- D4: A cohort below `cohorts.min_size` (including a remainder chunk) → individual review with `COHORT_BELOW_MIN_SIZE`.
- D5: The individual queue orders unknown priority (config gap) first, then priority descending, then case id.
- D6: Aliases are exact-match only. Case variants are not aliases.
- D7: The priority `notional` weight is per unit of notional (no hidden scale literal).

## UNKNOWNs
- U1: The original SCP CAL repo was not present. Real column names, types and the linking module are unknown; Phase 1 must reconcile them.
- U2: The real claim taxonomy and evidence requirements per category are placeholders in config.
- U3: The source for RFI events (derived from workflow events) needs its real table and semantics.
- U4: The past-case outcome vocabulary (`history.adverse_outcomes`) needs business confirmation.
- U5: The entitlement model for person fields is currently a boolean flag on `run`.

## Next step
Phase 1: design `SqlReadOnlySource` against the real SCP/CAL schema (needs U1 resolved).

## Audit (Phase 0)
invariant-auditor: PASS on R1–R16. Acted on note: invalid cohort sizes now fail safe explicitly (tested).
Open notes for Phase 1: `verify_claim` and `open_rfi` trust callers to pass PIT-filtered inputs; add boundary guards when the SQL source lands.
