"""The app log, document history and AI traceability (docs/observability.md)."""

from __future__ import annotations

import logging
import threading
from contextvars import copy_context

import pytest
from conftest import h
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_flows import Ingestor, add_document, add_job, settings
from test_usage import metered

from dps.advisory.advisor import Advisor
from dps.agent import usage
from dps.db import Base, Database
from dps.main import create_app
from dps.models import AiCall, AiUsage, Job
from dps.ops import observability as obs
from dps.worker import Worker


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'flows.db'}")
    Base.metadata.create_all(db.engine)
    return db


def test_secrets_never_reach_a_log_line():
    line = ('Authorization: Bearer abcdefghijklmnop password=Hunter22 "api_key": "sk-12345678" '
            "mssql+pymssql://sa:S3cret!@db:1433/dps eyJhbGciOi.eyJzdWIiOiJ4.c2lnbmF0dXJl client_secret=xyz")
    masked = obs.redact(line)
    for secret in ("abcdefghijklmnop", "Hunter22", "sk-12345678", "S3cret!", "eyJhbGciOi", "xyz"):
        assert secret not in masked, secret
    assert "sa:***@db" in masked and obs.redact("Bearer abcdefghijklmnop") == "Bearer ***"


def test_each_line_says_which_request_user_job_and_document_it_belongs_to(capsys):
    obs.setup("dps-test")
    obs.setup("dps-test")  # a second call adds no second handler
    with obs.bound(request_id="req-1", user="rm-anil", job_id="job-9", document_id="doc-7"):
        obs.event("FOUND", "DSCR >= 1.25 (page 4) password=x1")
    obs.event("REQUEST", "outside any request")
    err = capsys.readouterr().err.splitlines()
    assert any("[req-1 rm-anil] [job job-9 doc doc-7] dps.events: FOUND DSCR >= 1.25 (page 4) password=***" in x
               for x in err)
    assert any("[- -] [job - doc -] dps.events: REQUEST outside any request" in x for x in err)
    with obs.span("unit"):  # no OpenTelemetry endpoint: a no-op, never an error
        obs.count("dps.test", 1)
        obs.observe("dps.test.duration", 0.1)
    assert isinstance(obs.trace_headers(), dict)


def test_every_request_is_one_line_with_its_user_and_status(client, caplog):
    caplog.set_level(logging.INFO, logger="dps.events")
    client.get("/dps/v1/jobs", headers=h("support-1", "SUPPORT"))
    lines = [r for r in caplog.records if getattr(r, "event", None) == "REQUEST"]
    assert lines and lines[-1].getMessage().startswith("REQUEST GET /dps/v1/jobs 200")
    assert lines[-1].user == "support-1" and lines[-1].request_id != "-"
    assert lines[-1].status == 200


@pytest.fixture
def ai_client(settings, checker):
    return TestClient(create_app(settings, cp_checker=checker, create_schema=True, advisor=Advisor(metered)))


def test_a_failed_call_and_the_text_switch(settings):
    settings.ai_trace_text = True  # off by default (no document text in logs); switched on here to test the masking
    database = Database(settings.database_url)
    Base.metadata.create_all(database.engine)
    usage.note("azure/m", 10, 0, seconds=2.0, messages=[{"role": "system", "content": "You read loans"},
                                                        {"role": "user", "content": "token=abc123"}],
               error="Timeout: no answer")
    usage.note("azure/m", 5, 7, messages=[{"role": "user", "content": "q"}], answer="a")
    with database.session_factory() as db:
        usage.record(db, settings, "ASK_AI", "rm-anil")
        bad, good = db.scalars(select(AiCall).order_by(AiCall.prompt_tokens.desc())).all()
    assert (bad.status, bad.error, bad.seconds) == ("ERROR", "Timeout: no answer", 2.0)
    assert bad.prompt_version == usage.prompt_version([{"role": "system", "content": "You read loans"}]) != "-"
    assert "token=***" in bad.request_text and "abc123" not in bad.request_text
    assert (good.status, good.response_text, good.prompt_version) == ("OK", "a", "-")
    settings.ai_trace_text = False
    usage.note("azure/m", 1, 1, messages=[{"role": "user", "content": "q"}], answer="a")
    with database.session_factory() as db:
        usage.record(db, settings, "ASK_AI", "rm-anil")
        last = db.scalars(select(AiCall).where(AiCall.prompt_tokens == 1)).one()
    assert last.request_text is None and last.response_text is None


def test_the_ai_readers_of_a_document_are_counted_even_from_another_thread(database, tmp_path):
    """A flow may run its steps in another thread or context: the worker still records the job's model calls."""
    doc_id = add_document(database)
    job_id = add_job(database, doc_id)

    def runner(db, j: Job, model, raw):
        def read():
            usage.note("azure/m", 3000, 200, seconds=4.0, answer="DSCR >= 1.25")
        t = threading.Thread(target=copy_context().run, args=(read,))
        t.start()
        t.join()
        jobs.finish(db, j, jobs.DONE)

    from test_flows import FakeCore

    from dps import jobs

    assert Worker(settings(tmp_path), database.session_factory, FakeCore(), {}, Ingestor(),
                  pack_runner=runner).run_once()
    with database.session_factory() as s:
        [row] = s.scalars(select(AiUsage)).all()
        [call] = s.scalars(select(AiCall)).all()
    assert (row.purpose, row.job_id, row.document_id, row.prompt_tokens) == ("EXTRACT_AGREEMENT", job_id, doc_id, 3000)
    assert (call.user, call.seconds, call.response_text) == ("u", 4.0, None)  # no model text kept by default

