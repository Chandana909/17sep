# Security

## Threats and controls

| Threat | Control | Where | Tested |
|---|---|---|---|
| A caller claims another identity or role (e.g. to self-approve) | Identity only from a verified source: OIDC JWT (JWKS, asymmetric algorithms only, iss/aud/exp/iat) or an authenticating proxy carrying a shared secret; dev headers are refused in prod | `core/auth.py`, `core/readiness.py` | `tests/test_security.py` |
| One person releases a rule alone | Four-eyes: the submitter cannot approve; agents have no approval tool and act as `agent:*` principals without approval rights | `evolution/governance.py` | e2e |
| An authenticated user outside their remit | Roles and desks come from directory groups (`security.role_map`, `desk_map`); no mapped role means no access; desk entitlement is checked on every case and tool; person data needs its own group | `core/auth.py`, `core/security.py`, `agents/tools.py` | API tests |
| Prompt injection in alert text | Free text is fenced as untrusted, never executed; actions are schema-validated; conclusions must pass the verifier; a stuck or hijacked model hands over to the playbook | `engine/evidence.py`, `agents/*` | `test_llm_eval.py`, e2e |
| The LLM invents facts | Prose may cite evidence ids only; numbers outside citations are rejected; outcomes are deterministic | `agents/validation.py`, `engine/*` | agent tests |
| Tampering with history in the database | Append-only triggers (UPDATE/DELETE, and TRUNCATE on Postgres); the service role has no UPDATE/DELETE/TRUNCATE and does not own the tables; hash-chained audit log; Ed25519-signed chain heads in an external write-once sink catch even a consistently rebuilt chain | `store/*`, `deploy/postgres/roles.sql` | `test_storage.py`, `test_security.py` |
| Point-in-time leakage | Reads filter on `record_time <= as_of`; labels need `decided_at < as_of` | `store/db.py`, `engine/*` | engine tests |
| Quarantined data reaching decisions | Workflow and person fields live in annexes; decision modules may not reference them (static test) | `data/ingest.py`, `tests/test_architecture.py` | architecture tests |
| Web attacks on the console | Strict CSP (`script-src 'self'; style-src 'self'`), no inline scripts or styles, every value HTML-escaped, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, HSTS in prod, `no-store` on the API | `api/app.py`, `api/static/*` | architecture + API tests |
| Abuse and floods | Per-principal token-bucket rate limit (429 + Retry-After); request body size limit (413) | `core/ratelimit.py`, `api/app.py` | API tests |
| Leaking business data through telemetry | Metrics carry low-cardinality labels only (route templates, never ids); tested for trade, case and user identifiers | `core/metrics.py` | `test_observability.py` |
| Insecure deployment by mistake | `security.environment = "prod"` refuses to start on any readiness FAIL: dev auth, missing anchor key, non-HTTPS model or JWKS endpoints | `core/readiness.py` | `test_security.py` |
| Vulnerable dependencies | Hash-pinned production lockfile; `pip-audit --strict` in CI; non-root container, read-only filesystem in compose | `requirements/prod.lock`, `.github/workflows/ci.yml`, `Dockerfile` | CI |

## Authentication modes (`security.mode`)

- **`dev`:** `X-User` / `X-Roles` / `X-Desks` headers from the console's identity switcher. For a laptop only.
- **`proxy`:** an authenticating reverse proxy (oauth2-proxy, an API gateway) sets `X-Forwarded-User` and `X-Forwarded-Groups` and the shared secret header `X-ASAS-Proxy-Secret` (`$ASAS_PROXY_SECRET`). Requests without the secret are refused, so reaching the app directly cannot forge an identity. Keep the app on a private network behind the proxy as well.
- **`oidc`:** `Authorization: Bearer <JWT>` (or `security.oidc.token_header`), verified against the IdP's JWKS (cached). Configure `issuer`, `audience`, `jwks_url`, `algorithms` (asymmetric only), `user_claim` and `groups_claim`.

Map directory groups to roles and desks in `[security.role_map]` and `[security.desk_map]`. Person data (trader baselines) needs a group in `security.person_data_groups`.

## Secrets

Nothing secret lives in the repository. The environment supplies:
- `ASAS_DATABASE_URL`, and optionally `ASAS_DATABASE_READER_URL` for a SELECT-only role
- `ASAS_PROXY_SECRET` (proxy mode)
- `ASAS_AUDIT_SIGNING_KEY`: a path to, or the PEM of, the Ed25519 private key; generate it with `python -m asas audit keygen --out secrets`
- `ASAS_AUDIT_PUBLIC_KEY` for verification-only hosts
- `ASAS_LLM_API_KEY` if the model endpoint needs one

Values may be file paths, which suits mounted secrets.

## Production checklist

Run `python -m asas doctor --db "$ASAS_DATABASE_URL"`. Every line must be `OK`:
- [ ] `security.environment = "prod"`, `security.mode` = `oidc` or `proxy`
- [ ] Postgres with `deploy/postgres/roles.sql`: the service uses `asas_app`, and `asas migrate` runs as `asas_owner`
- [ ] `audit.anchor_dir` on storage the database account cannot modify (WORM share, object lock), and the key present
- [ ] model endpoint over HTTPS or local; `agents.fallbacks` configured
- [ ] Prometheus scraping `/metrics` on the private network, with `deploy/prometheus/alerts.yml` loaded
- [ ] a penetration test and a model-risk review scheduled (see `docs/operations.md`)
