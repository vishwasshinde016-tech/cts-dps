"""Logs, traces and metrics for the whole app.

Every log line carries who and what it is about: the request id (the same ``X-Correlation-Id`` the gateway gives
Core and DPS), the user, and for background work the job and document. Lines go to standard output, where the log
collector writes them into the daily file ``logs/cts-dps-YYYY-MM-DD.log``.

With ``OTEL_EXPORTER_OTLP_ENDPOINT`` set (the local stack points it at the Grafana container), the same lines, a
trace per request and the counters below also go to Grafana through OpenTelemetry. Without it (tests, CI) nothing
is sent and every call here costs next to nothing.

Passwords, tokens and keys never reach a log: ``redact`` masks them in every line.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("dps.events")

request_id: ContextVar[str] = ContextVar("dps_request_id", default="-")
user: ContextVar[str] = ContextVar("dps_user", default="-")
job_id: ContextVar[str] = ContextVar("dps_job_id", default="-")
document_id: ContextVar[str] = ContextVar("dps_document_id", default="-")
_VARS = {"request_id": request_id, "user": user, "job_id": job_id, "document_id": document_id}

# A secret written into a log is a leak: these are masked in every line, whatever wrote it.
_SECRETS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***"),
    (re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}"), "***jwt***"),
    (re.compile(r"((?:password|passwd|pwd|secret|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|client[_-]?secret"
                r"|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',&;]+", re.I), r"\1***"),
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+@"), r"\1***@"),  # user:password@host in a database URL
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class _Context(logging.Filter):
    """Adds the request, user, job and document to every record (``-`` when not known)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id, record.user = request_id.get(), user.get()
        record.job_id, record.document_id = job_id.get(), document_id.get()
        return True


class _Stderr(logging.StreamHandler):
    """Writes to the current ``sys.stderr`` (a stream swapped later, as by test runners, is followed)."""

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


class _Redacting(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


LINE_FORMAT = ("%(asctime)s %(levelname)-5s [%(request_id)s %(user)s] [job %(job_id)s doc %(document_id)s] "
               "%(name)s: %(message)s")

_otel: dict[str, Any] = {}


def setup(service: str) -> None:
    """Called once by each process (API, worker): log format, and OpenTelemetry when an endpoint is set."""
    root = logging.getLogger()
    if not any(getattr(h, "_dps", False) for h in root.handlers):
        handler = _Stderr()
        handler._dps = True  # type: ignore[attr-defined]
        handler.addFilter(_Context())
        handler.setFormatter(_Redacting(LINE_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and not _otel:
        _start_otel(service)


def _start_otel(service: str) -> None:  # pragma: no cover - needs a running collector
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service),
                                "service.namespace": "cts-dps"})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)]))
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    handler.addFilter(_Context())
    handler.addFilter(_RedactRecord())
    logging.getLogger().addHandler(handler)
    _otel["meter"] = metrics.get_meter("dps")
    _otel["instruments"] = {}


class _RedactRecord(logging.Filter):  # pragma: no cover - used with OpenTelemetry only
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        return True


def event(name: str, message: str, **fields: Any) -> None:
    """One business event (FOUND, AI_CALL, REVIEW ...) as a readable line; in Grafana its fields are searchable."""
    log.info("%s %s", name, message, extra={"event": name, **{k: v for k, v in fields.items() if v is not None}})


def count(name: str, value: float = 1, **attributes: Any) -> None:
    """Adds to a counter in Grafana (``dps.ai.tokens`` becomes ``dps_ai_tokens_total``)."""
    instrument = _instrument("counter", name)
    if instrument is not None:  # pragma: no cover - needs OpenTelemetry
        instrument.add(value, {k: str(v) for k, v in attributes.items()})


def observe(name: str, value: float, **attributes: Any) -> None:
    """Records a duration or size in a histogram in Grafana."""
    instrument = _instrument("histogram", name)
    if instrument is not None:  # pragma: no cover - needs OpenTelemetry
        instrument.record(value, {k: str(v) for k, v in attributes.items()})


def _instrument(kind: str, name: str):
    meter = _otel.get("meter")
    if meter is None:
        return None
    key = (kind, name)  # pragma: no cover - needs OpenTelemetry
    found = _otel["instruments"].get(key)  # pragma: no cover
    if found is None:  # pragma: no cover
        found = meter.create_counter(name) if kind == "counter" else meter.create_histogram(name, unit="s")
        _otel["instruments"][key] = found
    return found  # pragma: no cover


@contextmanager
def span(name: str, headers: dict[str, str] | None = None, **attributes: Any) -> Iterator[None]:
    """A trace step (one request, one job, one model call). ``headers`` continue the caller's trace."""
    from opentelemetry import context, propagate, trace

    token = context.attach(propagate.extract(headers)) if headers else None
    try:
        with trace.get_tracer("dps").start_as_current_span(name, attributes={
                k: v for k, v in attributes.items() if v is not None}):
            yield
    finally:
        if token is not None:
            context.detach(token)


def trace_headers() -> dict[str, str]:
    """``traceparent`` for an outgoing call, so Core's part of the work joins the same trace."""
    from opentelemetry import propagate

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def bound(**values: str | None) -> Iterator[None]:
    """Sets request_id / user / job_id / document_id for the lines written inside the block."""
    tokens = [(_VARS[k], _VARS[k].set(v)) for k, v in values.items() if v]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 3)
