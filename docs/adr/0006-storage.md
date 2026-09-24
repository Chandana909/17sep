# ADR 0006: Append-only SQLite store with Postgres-portable SQL

**Status:** accepted for release one

**Decision.**
- **Tables:** one SQLite file with WAL and immutability triggers on every table. Artifacts are content-hashed and versioned.
- **Audit:** the log is hash-chained.
- **Access:** tools and queries use `mode=ro` + `query_only` connections.

**Consequences.** Zero external infrastructure for evaluation and demos. The SQL is portable, so a Postgres (or bitemporal warehouse) adapter behind `Store` is the scale path. A graph database is not needed at this volume: the evidence graph is materialised per snapshot and queried with bounded BFS.
