# ADR 0010: Data capabilities and graceful degradation

**Status:** accepted

**Context.** Real SCP/CAL deployments differ: one lacks prices, another has no curated outcomes. Code that reads a missing field as "no problem" silently contradicts risk hypotheses; for example, no price looks like no price move.

**Decision.** `engine/capabilities.py` measures coverage of the measure fields in each point-in-time snapshot, plus fields the business declares unreliable (`data.unavailable_fields`).
- Hypotheses declare the fields they rely on and become INSUFFICIENT (`FIELD_UNAVAILABLE:*`) when those are missing.
- Deviation signals are skipped with a note.
- Rules that cannot fire are reported as coverage gaps.

The same model drives `asas data check/capabilities` and the console's Data tab.

**Consequences.** Missing data leads to abstention and individual review, never a guess. The integration report tells the business exactly which controls are degraded before go-live.
