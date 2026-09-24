# ADR 0007: Any OpenAI-compatible model, deterministic by default

**Status:** accepted

**Decision.**
- **Gateway:** a single `ModelGateway` protocol. The production adapter speaks the OpenAI chat-completions API, so it works with Qwen via Ollama or vLLM, LM Studio, DashScope or hosted models. It runs at temperature 0 with JSON-object output.
- **Resilience:** retries, a circuit breaker and a response cache.
- **Default:** `agents.enabled = false` runs the deterministic playbook.

**Consequences.** Small local models are safe to use: invalid or unsupported output is rejected or falls back, and conclusions are verified regardless of model quality.
