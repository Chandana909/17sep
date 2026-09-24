# ADR 0001: Agents propose, deterministic code decides

**Status:** accepted

**Context.** Surveillance decisions must be reproducible and defensible to a regulator. LLMs are strong at choosing what to look at and at reading text, and weak at computing, counting and remembering facts.

**Decision.**
- Agents choose hypotheses, evidence to retrieve, links to propose and candidates to draft.
- Deterministic code computes every fact, verifies every hypothesis, adjudicates conclusions, decides case treatment, and governs releases.
- Agent prose may not contain numbers except citations of evidence ids.

**Consequences.** With the model disabled, the deterministic playbook produces the same decisions. The model changes the investigation path, not the verdict. Test `test_12` shows identical decisions under a model outage.
