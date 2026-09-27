# ADR 0012: Externally anchored, signed audit chain heads

**Status:** accepted

**Context.** A hash chain inside the database proves internal consistency, but someone with write access could drop the triggers, rewrite an entry and recompute every later hash.

**Decision.**
- The chain head `(seq, hash)` is signed (Ed25519 by default, so auditors verify with the public key; HMAC as an alternative).
- It is written once per chain position to a sink the database account cannot modify.
- The service anchors after every pipeline run.
- Verification checks each anchor's signature and that the entry at `seq` still has that hash.

**Consequences.** A consistently rebuilt or truncated history is detected (tested), as is a fork at an anchored position. Production must provide a real write-once location (WORM share, object lock). Anchor failures do not fail a run but raise the unanchored-entries metric.
