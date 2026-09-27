# Playbooks

Step-by-step procedures for the changes the business will ask for. They are written so that a person or a small coding model (Qwen in OpenCode, for example) can follow them exactly. Each one lists the files to edit, the files never to touch, the commands to run, and what "done" means.

| When | Playbook |
|---|---|
| Real SCP/CAL extracts arrive | [integrate-real-data.md](integrate-real-data.md) |
| Numbers need tuning on real data | [calibrate-thresholds.md](calibrate-thresholds.md) |
| A rule or the bulk policy must change | [change-rules-and-policy.md](change-rules-and-policy.md) |
| A new measure of behaviour is needed | [add-signal.md](add-signal.md) |
| A new typology or benign explanation | [add-hypothesis.md](add-hypothesis.md) |
| The source has a field the contract lacks | [add-contract-field.md](add-contract-field.md) |
| Reviewer wording changes | [change-business-context.md](change-business-context.md) |
| Switch on or change the LLM | [swap-model.md](swap-model.md) |

After any playbook: `python scripts/check.py` must be green. The rails in `CLAUDE.md` / `AGENTS.md` always win over a playbook.
