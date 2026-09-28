# Integrating real data and a model

This page moved into step-by-step playbooks:

- **Real SCP/CAL extracts:** [playbooks/integrate-real-data.md](playbooks/integrate-real-data.md) covers `asas data profile` → `draft-mapping` → `check` (repeat until OK) → `ingest` → `pipeline`. It needs no code; the capability matrix tells the business which checks degrade with the fields available.
- **Thresholds on real data:** [playbooks/calibrate-thresholds.md](playbooks/calibrate-thresholds.md)
- **An LLM (Qwen via Ollama or vLLM, or any OpenAI-compatible endpoint):** [playbooks/swap-model.md](playbooks/swap-model.md), including the context-window setting and `asas llm-eval` before switching on.
- **Production hardening:** [security.md](security.md) and [deployment.md](deployment.md). These cover OIDC or a proxy, PostgreSQL roles, audit anchors, overlays, metrics and alerts.

Things to confirm with the business before production:
- Identifier semantics per source (TRADE_ID, ORIGINAL_TRADE_ID, ALTERNATE_TRADE_ID, URN_REF).
- Whether the source's system timestamp is a trustworthy RECORD_TIME. If you must `copy` it from EVENT_TIME, point-in-time guarantees weaken; record why.
- The hypothesis catalogue, its tolerances, and `[anomaly.explains]` (what each benign explanation accounts for).
- The initial bulk-policy scopes.
- The entitlement mapping from the identity provider (`[security.role_map]`, `[security.desk_map]`).
- The wording in `config/business_context.toml` (owned by Compliance).
