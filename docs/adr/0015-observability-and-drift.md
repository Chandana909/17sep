# ADR 0015: Prometheus metrics, health probes and run-KPI drift

**Status:** accepted

**Decision.**
- **Metrics:** one registry of low-cardinality metrics (route templates, never business ids), served at `/metrics`, with alert rules and runbook links in `deploy/prometheus/alerts.yml`.
- **Probes:** liveness does not touch dependencies; readiness checks storage, the active policy and the audit chain (cached).
- **Drift:** after each run, KPIs are compared with the median of recent runs (`[monitoring]`): bulk, escalation, abstention, unexplained, failure and fallback rates, and case and per-rule alert volumes.

**Consequences.** Silent failure modes become visible (a feed change, a policy release, a model regression). Drift informs and never changes decisions. Tests keep every alert rule tied to a real metric and an existing runbook section.
