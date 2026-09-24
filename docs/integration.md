# Integrating real data and a model

## Real SCP/CAL data

1. Export CSVs (UTF-8, header row), or implement a reader over your warehouse that produces the same contract rows (see `data/ingest.py`).
2. Copy `config/mapping.example.toml`.
   - Left-hand keys are your column names; right-hand values are contract fields (see [field-contract.md](field-contract.md)).
   - Every source column must be mapped or listed in `ignore`.
   - Code values go in `values` tables.
   - Timestamps without a zone need `naive_timestamp_offset`.
3. Ingest (idempotent; a row changed in place is rejected, because corrections must arrive as new versions):
   ```bash
   python -m asas ingest --db out/prod.db --data <dir> --mapping config/mapping.real.toml
   python -m asas pipeline --db out/prod.db --as-of 2026-05-05T00:00:00+00:00
   python -m asas serve --db out/prod.db
   ```
4. **Labels.** Only `LABEL_QUALITY = CURATED` outcomes are used by replay, counterexamples and discovery. Raw sign-offs are ingested but ignored, because they are not ground truth.
5. **Calibrate** `config/asas.toml` on your data: peer minimums, period-end cutoff, correction tolerances, discovery support, gates. Bump `version` on every change; it is stamped on every run manifest.

Things to confirm with the business before production:
- Identifier semantics per source (TRADE_ID, ORIGINAL_TRADE_ID, ALTERNATE_TRADE_ID, URN_REF).
- Whether `CREATED_AT` is a trustworthy record time.
- The hypothesis catalogue and its tolerances.
- The initial bulk-policy scopes.
- Entitlement mapping from your identity provider.

## A model (Qwen or any OpenAI-compatible endpoint)

```toml
[agents]
enabled = true
base_url = "http://localhost:11434/v1"   # Ollama; vLLM / LM Studio / DashScope work the same way
model_id = "qwen2.5:7b-instruct"
api_key_env = "ASAS_LLM_API_KEY"          # name of the env var, blank for local
```

The runtime calls the model at temperature 0 with JSON-object output and validates every action against the program's schema. Invalid output gets one repair attempt, after which the playbook takes the step. Outages open a circuit breaker. Conclusions are verified regardless of model quality, so a small local model cannot produce an unsupported decision. Responses are cached by request hash, so a run can be replayed from its record.

## Production hardening checklist

- Put an OIDC proxy in front of the API and map claims to `X-User`, `X-Roles`, `X-Desks` and `X-Person-Data`. Set `security.dev_auth = false` elsewhere.
- Move `Store` to Postgres (the SQL is portable). Keep the immutability triggers and the audit chain; give the API a role that cannot `UPDATE`/`DELETE`.
- Export spans with `OtlpJsonFileExporter` or an OTel collector; ship JSON logs.
- Keep the model endpoint on a network segment with no route to source systems.
