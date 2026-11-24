"""Tracing for Langfuse / any OpenTelemetry backend (observability, 23 Nov 2026).

Each model call is one trace span (model, tokens, prompt version, time, failure), inside the job's trace even when
CrewAI calls the model from its own thread. Prompt and answer text go on the span only with ``ai_trace_text`` on,
masked. A broken trace exporter never fails a model call.
"""

from __future__ import annotations

import contextvars
import threading

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from dps.agent import usage
from dps.agent.gateway import AiUnavailable, Gateway, Route, Routes, TransientModelError
from dps.ops import observability as obs

_EXPORTER = InMemorySpanExporter()


class _Gate(SimpleSpanProcessor):
    """Keeps spans only while a test here is running, so the rest of the suite does not fill memory."""

    active = False

    def on_end(self, span: ReadableSpan) -> None:
        if _Gate.active:
            super().on_end(span)


def _install() -> None:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    if not getattr(provider, "_dps_test_gate", False):
        provider.add_span_processor(_Gate(_EXPORTER))
        provider._dps_test_gate = True  # type: ignore[attr-defined]


@pytest.fixture
def spans():
    _install()
    _EXPORTER.clear()
    _Gate.active = True
    usage.begin()
    try:
        yield _EXPORTER
    finally:
        _Gate.active = False
        usage.take()


def _gateway(complete, trace_text: bool = False, retries: int = 0) -> Gateway:
    routes = Routes({"strong": Route("strong", "fake", "dep-strong", retries=retries)})
    return Gateway(routes, lambda _route: complete, sleep=lambda _s: None, trace_text=trace_text)


def _answer(prompt_tokens: int = 100, completion_tokens: int = 20, text: str = '{"items": []}'):
    def complete(messages):
        usage.note("gpt-test", prompt_tokens, completion_tokens, seconds=0.5, messages=messages, answer=text)
        return text

    return complete


def _model_spans(exporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]


def test_each_model_call_is_one_span_with_model_tokens_and_no_text(spans):
    gw = _gateway(_answer(1234, 56))
    gw.call("strong", [{"role": "system", "content": "Find covenants."}, {"role": "user", "content": "page text"}])
    [s] = _model_spans(spans)
    assert s.name == "chat dep-strong" and s.kind == SpanKind.CLIENT
    a = s.attributes
    assert a["gen_ai.operation.name"] == "chat" and a["gen_ai.request.model"] == "dep-strong"
    assert a["gen_ai.usage.input_tokens"] == 1234 and a["gen_ai.usage.output_tokens"] == 56
    assert a["gen_ai.usage.prompt_tokens"] == 1234 and a["gen_ai.response.model"] == "gpt-test"
    assert a["dps.route"] == "strong" and a["dps.attempt"] == 1 and len(a["dps.prompt_version"]) == 8
    # no text by default: the span carries ids, counts and times only
    assert "langfuse.observation.input" not in a and "langfuse.observation.output" not in a
    assert not any("page text" in str(v) for v in a.values())
    # the call's dps_ai_call row knows its span
    [call] = usage.last_call() and [usage.last_call()]
    assert call.trace_id == format(s.context.trace_id, "032x") and call.span_id == format(s.context.span_id, "016x")


def test_parse_duration_records_success_and_failure(spans, monkeypatch):
    from dps.ingest.pipeline import sandbox_parser
    from dps.parse import sandbox

    readings = []

    def parse_request(req, limits, hint):
        if not req.data:
            raise sandbox.ParseTimeout("Timed out")
        return {"pages": []}

    monkeypatch.setattr(sandbox, "run", parse_request)
    monkeypatch.setattr(obs, "observe", lambda name, seconds, **tags: readings.append((name, seconds, tags)))
    parse = sandbox_parser("STANDARD")
    with obs.span("test job"):
        assert parse(sandbox.ParseRequest(b"file", "PDF", "a"), 1) == {"pages": []}
        with pytest.raises(sandbox.ParseTimeout):
            parse(sandbox.ParseRequest(b"", "PDF", "a"), 1)
    assert len(readings) == 2
    assert all(name == "dps.parse.duration" and seconds >= 0 and tags == {"pool": "STANDARD", "transport": "process"}
               for name, seconds, tags in readings)
    parser_spans = [s for s in spans.get_finished_spans() if s.name == "dps.parse"]
    assert len(parser_spans) == 2
    assert parser_spans[1].attributes["error.type"] == "PARSE_TIMEOUT"


def test_spring_gets_trace_context_without_baggage(spans):
    import httpx
    from opentelemetry import baggage, context

    from dps.integrations.spring import SpringClient

    def respond(request):
        assert request.headers["traceparent"].split("-")[1:3] == [trace_id, span_id]
        assert "baggage" not in request.headers
        return httpx.Response(200, json={"currency": "INR"})

    token = context.attach(baggage.set_baggage("test", "private"))
    try:
        with httpx.Client(base_url="http://spring", transport=httpx.MockTransport(respond)) as client, obs.span("job"):
            trace_id, span_id = obs.current_ids()
            assert SpringClient("http://spring", 5, client=client).cp_currency("CP-1") == "INR"
    finally:
        context.detach(token)


def test_socket_parser_joins_the_worker_trace(spans, monkeypatch, tmp_path):
    from dps.parse import sandbox, server

    if not hasattr(server.socketserver, "ThreadingUnixStreamServer"):
        pytest.skip("parser service requires a Unix socket")
    path = str(tmp_path / "p.sock")
    monkeypatch.setattr(server, "handle", lambda header, body, pool, parser: {"ok": True, "model": {}})
    with server.socketserver.ThreadingUnixStreamServer(path, server._Handler) as listener:
        listener.pool = "STANDARD"
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        try:
            with obs.span("worker parse") as parent:
                assert server.SocketParser(path).parse(sandbox.ParseRequest(b"doc", "PDF", "a")) == {}
        finally:
            listener.shutdown()
            thread.join()
    [server_span] = [s for s in spans.get_finished_spans() if s.name == "dps.parser.handle"]
    assert server_span.context.trace_id == parent.get_span_context().trace_id
    assert server_span.parent.span_id == parent.get_span_context().span_id


def test_text_goes_on_the_span_only_with_the_switch_and_masked(spans):
    secret = "api_key=sk-live-1234567890abcdef and password: hunter22 for ravi@example.com"
    gw = _gateway(_answer(text='{"items": [], "note": "Bearer abcdefghijklmnop"}'), trace_text=True)
    gw.call("strong", [{"role": "user", "content": f"Covenant pages. {secret}"}])
    [s] = _model_spans(spans)
    sent, got = s.attributes["langfuse.observation.input"], s.attributes["langfuse.observation.output"]
    assert "Covenant pages." in sent
    for leaked in ("sk-live-1234567890abcdef", "hunter22", "abcdefghijklmnop"):
        assert leaked not in sent + got, leaked


def test_a_retried_attempt_is_its_own_failed_span(spans):
    state = {"n": 0}

    def flaky(messages):
        state["n"] += 1
        if state["n"] == 1:
            raise TransientModelError("429 rate limit")
        return _answer()(messages)

    assert _gateway(flaky, retries=1).call("strong", [{"role": "user", "content": "x"}]) == '{"items": []}'
    first, second = sorted(_model_spans(spans), key=lambda s: s.attributes["dps.attempt"])
    assert first.status.status_code == StatusCode.ERROR and first.attributes["error.type"] == "TransientModelError"
    assert second.status.status_code != StatusCode.ERROR and second.attributes["dps.attempt"] == 2


def test_a_failed_call_is_a_failed_span_and_the_error_is_masked(spans):
    def broken(_messages):
        raise RuntimeError("auth failed for password=topsecret99")

    with pytest.raises(AiUnavailable):
        _gateway(broken).call("strong", [{"role": "user", "content": "x"}])
    [s] = _model_spans(spans)
    assert s.status.status_code == StatusCode.ERROR and s.attributes["error.type"] == "RuntimeError"
    assert "topsecret99" not in s.attributes["error.message"]


def test_model_calls_from_crewai_threads_stay_in_the_jobs_trace(spans):
    """CrewAI runs an agent's model calls on its own threads; DPS's GatewayLLM copies the job's context there."""
    from dps.agent.document_agent import GatewayLLM

    gw = _gateway(_answer())
    with obs.span("dps.job") as job_span:
        llm = GatewayLLM(gw, "strong")  # made on the job's thread, as the Document Agent does
        worker = threading.Thread(target=lambda: llm.call("read the pages"))
        worker.start()
        worker.join()
    [s] = _model_spans(spans)
    assert s.context.trace_id == job_span.get_span_context().trace_id
    assert s.parent is not None and s.parent.span_id == job_span.get_span_context().span_id


def test_a_plain_thread_without_the_context_would_start_a_new_trace(spans):
    """Why the context copy matters: the same call on a bare thread is not in the job's trace."""
    gw = _gateway(_answer())
    with obs.span("dps.job") as job_span:
        worker = threading.Thread(target=lambda: gw.call("strong", [{"role": "user", "content": "x"}]))
        worker.start()
        worker.join()
        joined = threading.Thread(target=contextvars.copy_context().run,
                                  args=(gw.call, "strong", [{"role": "user", "content": "y"}]))
        joined.start()
        joined.join()
    bare, copied = sorted(_model_spans(spans), key=lambda s: s.start_time)
    assert bare.context.trace_id != job_span.get_span_context().trace_id
    assert copied.context.trace_id == job_span.get_span_context().trace_id


class _Broken(SpanExporter):
    def export(self, spans):
        raise ConnectionError("Langfuse is down")

    def shutdown(self):
        pass


def test_a_broken_trace_exporter_never_fails_a_model_call(spans):
    provider = trace.get_tracer_provider()
    gate = _Gate(_Broken())
    provider.add_span_processor(gate)  # type: ignore[attr-defined]
    try:
        assert _gateway(_answer()).call("strong", [{"role": "user", "content": "x"}]) == '{"items": []}'
    finally:
        gate.span_exporter = InMemorySpanExporter()  # neutralised for the rest of the run


@pytest.mark.parametrize(("env", "expected"), [
    ({}, set()),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, {"traces", "metrics", "logs"}),
    ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:3000/api/public/otel/v1/traces"}, {"traces"}),
    ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "x", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "y"}, {"traces", "metrics"}),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "x", "OTEL_SDK_DISABLED": "true"}, set()),
])
def test_which_signals_are_sent(env, expected):
    """Langfuse takes traces only: the laptop demo sets the traces endpoint alone."""
    assert obs.signals(env) == expected


def test_current_ids_outside_a_trace_are_none():
    assert obs.current_ids() == (None, None)


def test_a_finding_opens_its_ai_read_and_model_call_and_the_rows_open_the_trace(spans, tmp_path, monkeypatch):
    """Traceability end to end: an AI-only covenant -> its history row (job trace id, agent-read span id) -> the
    model call under that read -> the dps_ai_call row with the same span. The span id never reaches the result."""
    import json

    from sqlalchemy import select
    from test_flows import FakeCore, Ingestor, add_document, add_job, settings

    from dps import jobs
    from dps.db import Base, Database
    from dps.models import AiCall, TraceEvent
    from dps.skills import common, load
    from dps.skills import runner as skill_runner
    from dps.worker import Worker

    database = Database(f"sqlite:///{tmp_path / 'flows.db'}")
    Base.metadata.create_all(database.engine)
    doc_id = add_document(database)
    job_id = add_job(database, doc_id)
    pages = [{"no": 3, "text": "The Borrower shall maintain a DSCR of not less than 1.25 times."}]
    gw = _gateway(_answer(900, 40))
    kept: dict = {}

    def fake_read(pack, gateway, batch, extra="", notes=None, **kw):
        gateway.call("strong", [{"role": "user", "content": "the covenant pages"}])
        return [{"name": "DSCR", "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.25},
                 "page": 3, "quote": "shall maintain a DSCR of not less than 1.25 times"}]

    monkeypatch.setattr(common, "agent_proposals", fake_read)

    def pack_runner(db, job, model, raw):
        pack = load("covenants")
        proposals = common.batched_proposals(pack, gw, [pages])
        result = {"fields": skill_runner._agent_only(pack, pages, [], proposals)}
        skill_runner.trace_ai_fields(db, job, result)
        kept["result"] = result
        db.commit()
        jobs.finish(db, job, jobs.DONE)

    assert Worker(settings(tmp_path), database.session_factory, FakeCore(), {}, Ingestor(),
                  pack_runner=pack_runner).run_once()
    finished = {s.name: s for s in spans.get_finished_spans()}
    job_span, read_span = finished["dps.job"], finished["dps.ai.read covenants"]
    model_span = finished["chat dep-strong"]
    trace_hex = format(job_span.context.trace_id, "032x")
    assert read_span.context.trace_id == job_span.context.trace_id
    assert model_span.parent.span_id == read_span.context.span_id
    assert read_span.attributes["dps.proposals"] == 1 and read_span.attributes["dps.first_page"] == 3
    assert job_span.attributes["langfuse.session.id"] == doc_id and job_span.attributes["dps.status"] == jobs.DONE
    assert all("_aiSpan" not in f for f in kept["result"]["fields"])  # never sent to Spring
    with database.session_factory() as s:
        [row] = s.scalars(select(TraceEvent).where(TraceEvent.job_id == job_id, TraceEvent.step == "SHOWN")).all()
        [call] = s.scalars(select(AiCall).where(AiCall.job_id == job_id)).all()
    detail = json.loads(row.detail_json)
    assert row.trace_id == trace_hex and detail["ai_span_id"] == format(read_span.context.span_id, "016x")
    assert detail["field_id"] == kept["result"]["fields"][0]["id"] and detail["method"] == "AI"
    assert call.trace_id == trace_hex and call.span_id == format(model_span.context.span_id, "016x")
    assert (call.prompt_tokens, call.completion_tokens) == (900, 40)


def test_the_otel_lines_of_the_local_env_file_reach_the_process(tmp_path, monkeypatch):
    """setup.ps1 -Observability writes the Langfuse address into .env; Settings read only DPS_* keys from it."""
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "DPS_LLM_ENABLED=false",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:3000/api/public/otel/v1/traces",
        'OTEL_SERVICE_NAME="cts-dps"',
        "# OTEL_COMMENT=x",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS=already"]) + "\n", encoding="utf-8")
    for key in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SERVICE_NAME", "OTEL_COMMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "from-the-real-environment")
    added = obs.otel_from_env_file(str(env))
    import os

    assert sorted(added) == ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SERVICE_NAME"]
    assert os.environ["OTEL_SERVICE_NAME"] == "cts-dps"
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == "from-the-real-environment"  # a set variable wins
    assert "OTEL_COMMENT" not in os.environ and "DPS_LLM_ENABLED" not in added
    assert obs.otel_from_env_file(str(tmp_path / "missing.env")) == []
    assert obs.otel_from_env_file() == []  # the tests' DPS_OTEL_ENV_FILE="" : a laptop's dps/.env is never read
