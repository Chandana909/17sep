# ADR 0005: Candidate → replay → counterexamples → shadow → four-eyes → versioned release

**Status:** accepted

**Decision.**
- Candidates move through an append-only state machine. Each transition stores the hash of the report that justified it.
- Submission and approval require human principals; the approver must differ from the submitter.
- Approval creates a new immutable policy bundle and activates it. Rollback is an activation of an earlier bundle.

**Consequences.** Production detection can evolve weekly without anyone being able to skip evidence, and every historical decision can be replayed against the exact bundle that made it.
