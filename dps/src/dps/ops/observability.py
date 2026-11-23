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
from pathlib import Path
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
    otel_from_env_file()
    wanted = signals(os.environ)
    if wanted and not _otel:
        _start_otel(service, wanted)


def otel_from_env_file(path: str | None = None) -> list[str]:
    """Copies the ``OTEL_*`` lines of the local ``.env`` (written by scripts/setup.ps1 -Observability) into the process
    environment, which is where OpenTelemetry reads them; a variable already set wins. Settings read only the
    ``DPS_*`` keys, so without this a laptop's tracing settings would be ignored. Returns the keys it set.
    ``DPS_OTEL_ENV_FILE`` names another file; empty turns this off (the tests)."""
    if path is None:
        path = os.environ.get("DPS_OTEL_ENV_FILE", ".env")
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    added = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if not sep or not key.startswith("OTEL_") or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")
        added.append(key)
    return added


SIGNALS = ("traces", "metrics", "logs")


def signals(env) -> set[str]:
    """The signals to export: all three with ``OTEL_EXPORTER_OTLP_ENDPOINT`` (a collector), or each one with its own
    ``OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT``. Langfuse takes traces only (over HTTP, at /api/public/otel/v1/traces), so
    the laptop demo sets the traces endpoint alone and metrics and logs are not sent (observability, 23 Nov 2026).
    ``OTEL_SDK_DISABLED=true`` turns all of them off."""
    if str(env.get("OTEL_SDK_DISABLED", "")).lower() == "true":
        return set()
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return set(SIGNALS)
    return {s for s in SIGNALS if env.get(f"OTEL_EXPORTER_OTLP_{s.upper()}_ENDPOINT")}


def _start_otel(service: str, wanted: set[str] | None = None) -> None:  # pragma: no cover - needs a collector
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
    wanted = set(SIGNALS) if wanted is None else wanted
    if "traces" in wanted:
        # The batch processor exports on its own thread: a slow or stopped Langfuse/collector drops spans, it never
        # holds up a request or a job.
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
        import atexit

        atexit.register(tracer_provider.shutdown)  # a short script (demo, bulk check) sends its last spans on exit
    if "metrics" in wanted:
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)]))
        _otel["meter"] = metrics.get_meter("dps")
    if "logs" in wanted:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        handler.addFilter(_Context())
        handler.addFilter(_RedactRecord())
        logging.getLogger().addHandler(handler)
    _otel["signals"] = wanted
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
def span(name: str, headers: dict[str, str] | None = None, kind: str | None = None,
         **attributes: Any) -> Iterator[Any]:
    """A trace step (one request, one job, one model call). ``headers`` continue the caller's trace; ``kind="client"``
    marks a call out (a model call). Yields the span, so the caller can add what it learns later (``set_attrs``).
    An exception leaving the block marks the span as failed, with ``error.type``."""
    from opentelemetry import context, propagate, trace

    token = context.attach(propagate.extract(headers)) if headers else None
    span_kind = trace.SpanKind.CLIENT if kind == "client" else trace.SpanKind.INTERNAL
    try:
        # record_exception off: a stack trace can quote document text; the type and a masked, short message are kept.
        with trace.get_tracer("dps").start_as_current_span(name, kind=span_kind, attributes=_clean(attributes),
                                                           record_exception=False) as s:
            try:
                yield s
            except BaseException as ex:
                attrs = getattr(s, "attributes", None) or {}
                if "error.type" not in attrs:  # the first cause wins (mark_failed inside the block)
                    s.set_attribute("error.type", getattr(ex, "code", None) or type(ex).__name__)
                    s.set_attribute("error.message", redact(str(ex))[:300])
                raise
    finally:
        if token is not None:
            context.detach(token)


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    """Span attributes OpenTelemetry accepts: no None; other values than str/bool/int/float become text."""
    out: dict[str, Any] = {}
    for k, v in attributes.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, bool, int, float)) else str(v)
    return out


def set_attrs(target: Any = None, **attributes: Any) -> None:
    """Adds attributes to ``target`` (a span from ``span``) or to the current span; nothing without tracing."""
    from opentelemetry import trace

    s = target if target is not None else trace.get_current_span()
    if s is not None and s.is_recording():
        s.set_attributes(_clean(attributes))


def mark_failed(target: Any, error: BaseException | str) -> None:
    """Marks ``target`` failed for an error handled inside its block (a model call that will be retried)."""
    from opentelemetry.trace import Status, StatusCode

    if target is None or not target.is_recording():
        return
    code = error if isinstance(error, str) else (getattr(error, "code", None) or type(error).__name__)
    target.set_attribute("error.type", code)
    if not isinstance(error, str):
        target.set_attribute("error.message", redact(str(error))[:300])
    target.set_status(Status(StatusCode.ERROR, code))


def current_ids() -> tuple[str | None, str | None]:
    """(trace id, span id) of the current span as hex, or (None, None) outside a recorded trace. Stored on
    ``dps_ai_call`` and ``dps_trace_event`` so a database row opens its trace in Langfuse, and back."""
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


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
