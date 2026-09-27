# Deployment

## Shapes

| Shape | Storage | Identity | Use |
|---|---|---|---|
| Laptop | SQLite file | dev headers | demos, development, integrating extracts |
| `docker compose up` | PostgreSQL 16 | dev headers | a production-like local stack (migrations job, anchors volume, read-only container) |
| Production | PostgreSQL (managed), least-privilege roles | OIDC or authenticating proxy | the service behind TLS, Prometheus, external anchor sink |

## Configuration

`config/asas.toml` is the single reviewed source of every threshold. Environments differ only through **overlays**, which are deep-merged on top:

```bash
export ASAS_CONFIG_OVERLAY=/app/config/overlays/prod.toml   # os.pathsep-separated for several
export ASAS_CONFIG_DIR=/app/config                           # where asas.toml, business context, rulesets live
```

Start from `config/overlays/prod.example.toml`. The merged configuration's fingerprint is stamped on every run manifest.

## PostgreSQL

1. Create the database, then run `deploy/postgres/roles.sql` as a DBA with `psql -v owner_password=... -v app_password=... -v reader_password=...`.
2. Migrate as the owner:
   ```bash
   ASAS_DATABASE_URL=postgresql://asas_owner:...@db/asas python -m asas migrate
   ```
3. Run the service as `asas_app` with `storage.auto_migrate = false`. It verifies the schema is current and refuses to start otherwise. Readers can use `ASAS_DATABASE_READER_URL` (`asas_reader`).
4. **Backups:** point-in-time recovery on the managed database. Everything is append-only, so a restore never needs reconciliation beyond replaying after the restore point.
5. **Retention:** keep the store and the anchor sink for your record-keeping obligation (often 5–7 years).

Schema changes are new migrations in `store/migrations.py`; a released one is never edited, and edits are detected by checksum.

## Container

```bash
docker build -t asas .
docker run --rm -p 8000:8000 \
  -e ASAS_DATABASE_URL=postgresql://asas_app:...@db/asas \
  -e ASAS_CONFIG_OVERLAY=/app/config/overlays/prod.toml \
  -e ASAS_AUDIT_SIGNING_KEY=/run/secrets/audit_signing_key \
  -v /mnt/worm/asas-anchors:/var/lib/asas/anchors asas
```

- **Image:** hash-pinned dependencies, non-root (uid 10001), a health check on `/api/health/live`, suitable for a read-only filesystem.
- **Probes:** liveness `/api/health/live`; readiness `/api/health/ready` (storage, active policy, audit chain).
- **Scaling:** the API is stateless apart from its per-process rate limiter and caches. Run several replicas, and keep the gateway's global rate limit as the primary one. Run the daily pipeline as one scheduled job:
  ```bash
  asas pipeline && asas challenge && asas audit anchor
  ```
  Use your scheduler (cron, Airflow, Control-M). Set `pipeline.max_workers` for LLM runs. Postgres advisory locks keep the audit chain linear across replicas and jobs.

## Local stack

```bash
python -m asas audit keygen --out secrets
docker compose up --build
```

Then open http://localhost:8000. The compose overlay (`config/overlays/compose.toml`) uses Postgres and anchors with dev identities.

## Observability

- **Metrics:** `/metrics` (Prometheus text). Alert rules are in `deploy/prometheus/alerts.yml`; each links a runbook section in `docs/operations.md`.
- **Logs:** JSON lines on stderr with `trace_id`, `span_id` and `request_id`.
- **Traces:** spans are stored with every run (`GET /api/runs/{id}`) and exportable as OTLP/JSON.

## CI

`.github/workflows/ci.yml` runs:
1. `scripts/check.py` on Python 3.11 and 3.12, against a real PostgreSQL service (`ASAS_TEST_DATABASE_URL`)
2. `pip-audit` on the production lockfile
3. the container build, with a smoke run of generate → ingest → pipeline → verify-audit inside the image

Refresh the lockfiles with:

```bash
uv pip compile pyproject.toml --extra data --extra oidc --extra postgres --generate-hashes --python-version 3.11 --python-platform linux -o requirements/prod.lock
uv pip compile pyproject.toml --extra dev --extra data --extra oidc --extra postgres --python-version 3.11 -o requirements/dev.lock
```
