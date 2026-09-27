# ADR 0013: SQLite and PostgreSQL dialects, versioned migrations, least-privilege roles

**Status:** accepted (supersedes ADR 0006 for storage)

**Decision.**
- **Dialects:** `store/dialect.py` isolates connections, placeholders, "insert unless present", error translation and cross-process audit locking: SQLite `BEGIN IMMEDIATE`, Postgres `pg_advisory_xact_lock`.
- **Migrations:** the schema is declared once and applied as checksummed migrations (`store/migrations.py`). Append-only triggers cover UPDATE and DELETE on both dialects, plus TRUNCATE on Postgres.
- **Roles:** in production the owner role migrates, and the service role has SELECT+INSERT only and does not own the tables (`deploy/postgres/roles.sql`). The service starts with `auto_migrate = false`.

**Consequences.**
- The full lifecycle runs identically on SQLite and PostgreSQL 16 (tested end to end).
- Raw-SQL tampering by the service role is denied (tested).
- Two writers on one SQLite file can no longer fork the audit chain, a bug found by the new contract tests.
