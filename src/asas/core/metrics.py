"""Prometheus metrics: the numbers operations alert on (deploy/prometheus/alerts.yml).

All metrics live in one dedicated registry and carry low-cardinality labels only. They never
carry trade, alert, person or free-text values; the API serves them at `/metrics`.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

BUILD = Gauge(
    "asas_build_info", "Build and config identity", ["version", "config"], registry=REGISTRY
)

HTTP_REQUESTS = Counter(
    "asas_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
HTTP_SECONDS = Histogram("asas_http_request_seconds", "HTTP latency", ["route"], registry=REGISTRY)
AUTH_FAILURES = Counter(
    "asas_auth_failures_total", "Rejected authentications", ["reason"], registry=REGISTRY
)

PIPELINE_RUNS = Counter("asas_pipeline_runs_total", "Pipeline runs", ["outcome"], registry=REGISTRY)
PIPELINE_SECONDS = Histogram(
    "asas_pipeline_seconds",
    "Pipeline wall time",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
    registry=REGISTRY,
)
PIPELINE_LAST_SUCCESS = Gauge(
    "asas_pipeline_last_success_timestamp",
    "Unix time of the last successful run",
    registry=REGISTRY,
)
CASES = Gauge("asas_cases", "Cases in the latest run", ["recommendation"], registry=REGISTRY)
DATA_GATE_FAILURES = Counter(
    "asas_data_gate_failures_total", "Failed data gates", ["gate"], registry=REGISTRY
)
DATA_FRESHNESS = Gauge(
    "asas_data_freshness_seconds",
    "Age of the newest record at run time",
    ["entity"],
    registry=REGISTRY,
)
SAFE_MODE = Gauge("asas_safe_mode", "Ops safe mode (1 = on)", ["kind"], registry=REGISTRY)

AGENT_RUNS = Counter(
    "asas_agent_runs_total", "Completed agent runs", ["agent", "policy"], registry=REGISTRY
)
AGENT_FALLBACKS = Counter(
    "asas_agent_fallbacks_total", "Steps taken by the playbook after a model problem",
    ["agent"], registry=REGISTRY,
)  # fmt: skip
MODEL_CALLS = Counter("asas_model_calls_total", "Model calls", ["agent"], registry=REGISTRY)
MODEL_ERRORS = Counter("asas_model_errors_total", "Model errors", ["agent"], registry=REGISTRY)
TOOL_CALLS = Counter("asas_tool_calls_total", "Tool calls", ["tool", "outcome"], registry=REGISTRY)
TOOL_SECONDS = Histogram(
    "asas_tool_seconds", "Tool latency", ["tool"],
    buckets=(0.001, 0.005, 0.02, 0.1, 0.5, 2, 10), registry=REGISTRY,
)  # fmt: skip
CIRCUIT_OPEN = Gauge(
    "asas_model_circuit_open", "Model circuit breaker open (1)", ["model"], registry=REGISTRY
)

AUDIT_VALID = Gauge("asas_audit_chain_valid", "Audit chain verifies (1)", registry=REGISTRY)
AUDIT_ENTRIES = Gauge("asas_audit_entries", "Audit log entries", registry=REGISTRY)
AUDIT_UNANCHORED = Gauge(
    "asas_audit_unanchored_entries",
    "Audit entries after the last external anchor",
    registry=REGISTRY,
)
DRIFT = Gauge("asas_drift_flag", "Drift detected on a run KPI (1)", ["metric"], registry=REGISTRY)


def exposition() -> bytes:
    return generate_latest(REGISTRY)
