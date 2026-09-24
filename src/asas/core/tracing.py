"""OpenTelemetry-compatible tracing without a hard SDK dependency.

Spans carry W3C trace/span ids, nanosecond timestamps, attributes and status, and export to
OTLP/JSON (`resourceSpans`) so any OTel collector can ingest them.
"""

from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SERVICE_NAME = "asas"
_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("asas_span", default=None)


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    status_message: str = ""

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def to_otlp(self) -> dict[str, Any]:
        def attr(k: str, v: Any) -> dict[str, Any]:
            if isinstance(v, bool):
                return {"key": k, "value": {"boolValue": v}}
            if isinstance(v, int):
                return {"key": k, "value": {"intValue": str(v)}}
            if isinstance(v, float):
                return {"key": k, "value": {"doubleValue": v}}
            return {"key": k, "value": {"stringValue": str(v)}}

        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": [attr(k, v) for k, v in sorted(self.attributes.items())],
            "status": {"code": 1 if self.status == "OK" else 2, "message": self.status_message},
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        return span


class SpanExporter(Protocol):
    def export(self, span: Span) -> None: ...


class InMemoryExporter:
    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    def export(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)


class OtlpJsonFileExporter:
    """Appends one OTLP/JSON `resourceSpans` document per span (collector file receiver format)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def export(self, span: Span) -> None:
        doc = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": SERVICE_NAME}}
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": SERVICE_NAME}, "spans": [span.to_otlp()]}],
                }
            ]
        }
        with self._lock, open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")


class Tracer:
    def __init__(self, exporters: list[SpanExporter] | None = None) -> None:
        self._exporters = exporters or []

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = _current.get()
        span = Span(
            trace_id=parent.trace_id if parent else secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            parent_span_id=parent.span_id if parent else None,
            name=name,
            start_ns=time.time_ns(),
            attributes=dict(attributes),
        )
        token = _current.set(span)
        try:
            yield span
        except Exception as exc:
            span.status = "ERROR"
            span.status_message = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.end_ns = time.time_ns()
            _current.reset(token)
            for exporter in self._exporters:
                exporter.export(span)


def current_span_ids() -> tuple[str | None, str | None]:
    span = _current.get()
    return (span.trace_id, span.span_id) if span else (None, None)


NOOP_TRACER = Tracer()
