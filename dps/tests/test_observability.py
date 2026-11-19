"""The app log, document history and AI traceability (docs/observability.md)."""

from __future__ import annotations

import logging
import threading
from contextvars import copy_context

import pytest
from conftest import h
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_api import add_term, session, upload
from test_flows import AGREEMENT_PAGES, FakeCore, _kickoff, add_document, add_job, intake, settings
from test_usage import GOLDEN, metered

from dps.advisory.advisor import Advisor
from dps.agent import usage
from dps.db import Base, Database
from dps.flows.persistence import SqlFlowPersistence
from dps.main import create_app
from dps.models import AiCall, AiUsage, Job, TraceEvent
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
    upload(client)
    lines = [r for r in caplog.records if getattr(r, "event", None) == "REQUEST"]
    assert lines and lines[-1].getMessage().startswith("REQUEST POST /dps/v1/documents 202")
    assert lines[-1].user == "rm-anil" and lines[-1].request_id != "-"
    assert lines[-1].status == 202


def test_a_document_history_shows_what_was_read_found_and_decided(database, tmp_path):
    doc_id = add_document(database)
    job_id = add_job(database, doc_id)
    w = Worker(settings(tmp_path), database.session_factory, FakeCore(),
               {"AGREEMENT_INTAKE": lambda j: _kickoff(intake(database, AGREEMENT_PAGES), j)})
    assert w.run_once()
    with database.session_factory() as s:
        steps = s.scalars(select(TraceEvent).where(TraceEvent.document_id == doc_id).order_by(TraceEvent.at)).all()
    kinds = [x.step for x in steps]
    assert kinds[0] == "READ" and kinds[-1] == "RESULT" and "FOUND" in kinds
    assert steps[0].summary.endswith("(standard reader)") and "pages:" in steps[0].summary
    found = next(x for x in steps if x.step == "FOUND" and x.summary.startswith("Minimum DSCR"))
    assert ">= 1.25" in found.summary and "page" in found.summary
    assert any(x.summary.startswith("ICR >= 2 (page 1)") for x in steps)
    assert all(x.job_id == job_id for x in steps)


def test_the_history_endpoint_lists_uploads_reviews_and_hides_prompts_from_rms(client, app):
    doc = upload(client).json()["documentId"]
    term = add_term(app, doc, name="DSCR", expression="DSCR >= 1.25", operator=">=", threshold=1.25,
                    status="NEEDS_REVIEW")
    assert client.patch(f"/dps/v1/terms/{term}", json={"action": "EDIT", "expression": "DSCR >= 1.30",
                                                       "note": "per amendment"}, headers=h()).status_code == 200
    with session(app) as s:
        s.add(AiCall(purpose="AGREEMENT_READ", user="rm-anil", document_id=doc, model="azure/m", prompt_version="ab12",
                     status="OK", seconds=1.5, request_text="pages 1-3", response_text="DSCR >= 1.25"))
        s.commit()
    rm = client.get(f"/dps/v1/documents/{doc}/history", headers=h()).json()
    assert [x["step"] for x in rm["steps"]] == ["UPLOAD", "REVIEW"]
    assert rm["steps"][0]["summary"].startswith('rm-anil uploaded "agreement.pdf" (LOAN_AGREEMENT) for CP-1')
    assert rm["steps"][1]["summary"] == ('rm-anil edited DSCR: DSCR >= 1.25 -> DSCR >= 1.30 ("per amendment")')
    assert rm["aiCalls"][0]["model"] == "azure/m" and rm["aiCalls"][0]["requestText"] is None
    auditor = client.get(f"/dps/v1/documents/{doc}/history", headers=h("auditor-1", "AUDITOR")).json()
    assert auditor["aiCalls"][0]["requestText"] == "pages 1-3"
    assert client.get(f"/dps/v1/documents/{doc}/history", headers=h("rm-other")).status_code == 404


@pytest.fixture
def ai_client(settings, checker):
    return TestClient(create_app(settings, cp_checker=checker, create_schema=True, advisor=Advisor(metered)))


def test_every_model_call_is_traced_with_its_prompt_version_and_answer(ai_client):
    doc = upload(ai_client, data=GOLDEN.read_bytes(), name="AGR-003.pdf").json()["documentId"]
    ai_client.post(f"/dps/v1/documents/{doc}/ask", json={"question": "What is the minimum DSCR?"}, headers=h())
    with ai_client.app.state.db.session_factory() as db:
        [call] = db.scalars(select(AiCall)).all()
    assert (call.purpose, call.user, call.document_id, call.status) == ("AGREEMENT_QA", "rm-anil", doc, "OK")
    assert (call.prompt_tokens, call.completion_tokens) == (1200, 80)
    assert call.request_id and call.request_id != "-"


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

    def runner(j: Job):
        def read():
            usage.note("azure/m", 3000, 200, seconds=4.0, answer="DSCR >= 1.25")
        t = threading.Thread(target=copy_context().run, args=(read,))
        t.start()
        t.join()
        return "PUBLISHED", "state-1", ""

    assert Worker(settings(tmp_path), database.session_factory, FakeCore(), {"AGREEMENT_INTAKE": runner}).run_once()
    with database.session_factory() as s:
        [row] = s.scalars(select(AiUsage)).all()
        [call] = s.scalars(select(AiCall)).all()
    assert (row.purpose, row.job_id, row.document_id, row.prompt_tokens) == ("AGREEMENT_READ", job_id, doc_id, 3000)
    assert (call.user, call.seconds, call.response_text) == ("u", 4.0, None)  # no model text kept by default


def test_a_resumed_flow_always_loads_its_latest_state(database):
    """Checkpoints saved in the same clock tick were ordered by a random id, so a resume could load an older one."""
    p = SqlFlowPersistence(database.session_factory)
    for n in range(30):
        p.save_state("u1", f"step{n}", {"n": n})
    assert p.load_state("u1") == {"n": 29}


def test_a_statement_history_lists_each_figure_with_its_line(database):
    from test_flows import GOOD_ROWS, statement, xlsx_bytes

    _, doc_id = statement(database, xlsx_bytes(GOOD_ROWS), "fs.xlsx")
    with database.session_factory() as s:
        steps = s.scalars(select(TraceEvent).where(TraceEvent.document_id == doc_id)).all()
    summaries = [f"{x.step} {x.summary}" for x in steps]
    assert "READ statement in lakh; figures stored in INR crore" in summaries
    assert 'VALUE EBITDA = 5.00 (line "EBITDA")' in summaries
    assert "VALUE TOTAL_DEBT = 10.00 (derived)" in summaries
    assert summaries[-1].startswith("RESULT REPORTED: ") and "CP-1" in summaries[-1]
