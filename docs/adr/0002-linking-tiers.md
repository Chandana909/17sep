# ADR 0002: Two-stage canonical linking with a quarantined agent overlay

**Status:** accepted

**Decision.**
- Strong lineage (same trade, `ORIGINAL_TRADE_ID`) forms the core.
- Medium keys (`URN_REF`, `ALTERNATE_TRADE_ID`) attach only under guards: degenerate-value blocklist, fan-out limit, time gap, same instrument, maximum span.
- Unproven relationships become residue pairs. The Investigation Agent may propose a link; the verifier checks its economics; it stays VERIFIED (quarantined) until an analyst confirms it, and only then joins the canonical overlay.

**Consequences.** Linking precision is never traded for recall by the model; recall improves through human-confirmed proposals (0.80 → 1.00 on synthetic data).
