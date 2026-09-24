# Governance and evolution

## Who can do what

| Action | Role | Notes |
|---|---|---|
| Ingest, run pipeline | admin / service | audited |
| Investigate, challenge, discover, replay, counterexamples, shadow | investigator, approver, service | agents act with the requester's data scope but never their approval rights |
| Confirm an agent-proposed link | investigator, approver (human) | only VERIFIED proposals; becomes a canonical overlay edge |
| Record a case decision | investigator, approver | stored as a RAW outcome in ASAS; SCP/CAL are never written |
| Curate an outcome | approver, not the decider (four-eyes) | only CURATED outcomes are ever used as labels |
| Submit a candidate | investigator, approver (human) | only after SHADOW_PASSED |
| Approve a candidate | approver, not the submitter (four-eyes) | creates and activates a new policy bundle |
| Reject / roll back | approver | rollback = activate an earlier bundle |

Agents (`agent:*` principals) and service principals are rejected by every governance method. There is no tool for any of these actions.

## Candidate lifecycle

```
DRAFT ─replay─► REPLAYED ─attack─► COUNTEREXAMPLES_PASSED ─shadow─► SHADOW_PASSED
                                    └► COUNTEREXAMPLES_FAILED        └► SHADOW_FAILED
SHADOW_PASSED ─submit (human)─► AWAITING_APPROVAL ─approve (other human)─► RELEASED
any non-terminal state ─reject (approver)─► REJECTED
```

Each transition is an append-only event carrying the hash of the report that justified it. The same transition also appears in the hash-chained audit log.

## Gates (all thresholds in `config/asas.toml`)

**Replay** (`replay.*`) compares production with production plus the candidate. Each episode is evaluated as of `end + evaluation_delay_hours`, over the window before the shadow period. It reports current, candidate and combined precision and recall on **curated** labels, plus added, lost and volume change.

**Counterexamples** (`counterexamples.*`) cover:
- false positives on curated cleared history
- false negatives on the rule's own target typology
- boundary cases near numeric thresholds
- mutation survivors (conditions that do nothing)
- near-misses
- temporal instability across the window's halves

A candidate fails on low precision, too few true positives, too many false positives, dead conditions, instability or low target recall.

**Shadow** (`shadow.*`) runs the candidate beside production on the most recent, unlabelled window, with no side effects. It fails if the candidate adds too many alerts per day or its new alerts are rarely corroborated by investigation.

**Bulk-policy candidates** fail if any curated escalation would have been routed to bulk review, or if there is too little curated clean history for the scope.

## Policy bundles

A bundle contains the detection rule set and the bulk-review policy. Bundles are immutable and versioned (`BUNDLE-0001`, `BUNDLE-0002`, and so on), and each records its parent, creator and source candidate. The active bundle is the latest activation. Historical cases keep the bundle id they were decided under, so any decision can be replayed exactly.
