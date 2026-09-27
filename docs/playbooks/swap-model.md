# Playbook: switch on or change the LLM (Qwen via Ollama, vLLM or any OpenAI-compatible endpoint)

**The contract:** the model chooses the investigation path; the deterministic verifier decides every outcome. A weaker model costs time (fallbacks, more steps); it cannot produce an unsupported conclusion or a false bulk proposal. Measure the cost before switching on.

## 1. Serve the model

Ollama on a workstation:

```bash
ollama pull qwen2.5:7b-instruct
```

**Context window:** ASAS observations are up to `agents.max_prompt_chars` characters, about a quarter as many tokens. Make sure the server context is at least 8k tokens: start Ollama with `OLLAMA_CONTEXT_LENGTH=16384`, or build a variant with a Modelfile containing `PARAMETER num_ctx 16384`. Ollama's default of 4096 silently drops the start of long prompts, including the system instructions. Otherwise, lower `agents.max_prompt_chars` to about 9000. The runtime then drops the oldest evidence and keeps the JSON valid.

For production, use vLLM or an internal gateway serving an instruct model (for example Qwen2.5-14B/32B-Instruct) over HTTPS.

## 2. Evaluate before enabling

```bash
python -m asas llm-eval --model qwen2.5:7b-instruct --base-url http://localhost:11434/v1 --per-scenario 2 --report docs/llm-evaluation.md --json out/llm-eval.json
```

Read the summary:

| Metric | Want |
|---|---|
| `agreement_with_playbook` | all cases (the verifier guarantees correctness; disagreement means the model ended in an abstention the playbook avoided, or ran out of budget) |
| `valid_action_rate` | ≥ 0.9; below that the model mostly costs fallbacks |
| `rejected_conclusions` | low; high means the model guesses conclusions |
| `injection_resisted` | true |
| `latency_per_call_p95_s` × steps | fits your daily run window at your case volume with `pipeline.max_workers` |

## 3. Configure

In `config/asas.toml` (or an overlay):

```toml
[agents]
enabled = true
base_url = "http://localhost:11434/v1"
model_id = "qwen2.5:7b-instruct"
api_key_env = ""                 # env var name holding a key, blank for local
fallbacks = [{ base_url = "http://gpu-2:8000/v1", model_id = "qwen2.5-14b-instruct" }]
```

Also set `[pipeline] max_workers = 4`, since investigations are I/O-bound on the model.

The model id is part of every run's idempotency key: runs are never replayed across models. `python -m asas doctor` fails in production for a non-local endpoint without HTTPS.

## 4. Operate

- Model incident: open the console Overview as admin, tick "suspend LLM" and give a reason, or run `python -m asas ops set --db ... --bulk resume --llm suspend --reason "model incident"`. Agents run on the playbook and outcomes are unchanged.
- Alerts: `AsasModelCircuitOpen` and `AsasHighFallbackRate` (`deploy/prometheus/alerts.yml`).
- Re-run `llm-eval` on every model or prompt version change. The prompt digest is in every run manifest.
