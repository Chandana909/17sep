# Agents spec

## 1. Role
Agents draft prose only: case summaries, cohort summaries and RFI drafts. They decide nothing (rail 5).

## 2. Access
LLM calls go only through `ModelGateway.complete(ModelRequest)`. Tests use `FakeModelGateway` (fixture per task).

## 3. Inputs
Each request carries the system prompt, fact ids with labels (never values), and untrusted free text wrapped by `delimit()`.

## 4. Output validation (rail 4)
`validate_prose`: every `{{F<n>}}` must reference a known fact. No numeric character (any Unicode `isnumeric`) may appear outside placeholders, except tokens in `agents.digit_whitelist`. Malformed braces and empty output are rejected.

## 5. Fallback
On a validation failure or gateway exception, the output is the deterministic template rendered with the same facts.

## 6. Tools (rail 13)
The closed `ToolRegistry` exposes only read-only lookups over the run snapshot (`get_case`). The agent package may not import I/O, network, process or DB modules (enforced by a static test).

## 7. Decision-environment manifest (rail 14)
One `DecisionManifest` per invocation, appended to `ManifestLog`:
`invocation_id, task, subject_id, model_id, prompt_version, config_version, field_contract_version, as_of, input_hash, output_hash, tools, fact_ids, validation_errors, fallback_used`.

## 8. Enablement (rail 15)
An agent runs only if `agents.enabled = true` **and** a gateway is supplied. Missing agents config → disabled.
