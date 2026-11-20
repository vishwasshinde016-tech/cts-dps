"""Full review WP7 (one pipeline, no Covenexa) and the remaining operations items (G5, G24).

The IDs are the finding IDs in FIX-PLAN.md.
"""

from __future__ import annotations

import importlib.util
import os

import pytest
from conftest import h
from sqlalchemy import select
from test_phase4_skills import submit

from dps import feed, jobs
from dps.api.jobs import JOB_TYPES
from dps.models import Job, LabelMemory, StatementValue
from dps.skills import runner as skills
from dps.worker import Worker, statement_deps

SERVICE = h("cts-spring", "SERVICE")
STATEMENT = (b"Amounts in INR crore\nLine item,FY26,FY25\nEBITDA,120,100\nShort-term borrowings,150,140\n"
             b"Long-term borrowings,250,230\nTotal equity,900,800\nCash and cash equivalents,40,30\n")


class Core:
    """What the statement reader asks Core: the loan's currency, FX rates and the borrower profile."""

    def cp_currency(self, cp):
        return "INR"

    def fx_rate(self, src, dst, on):
        return None

    def cp_profile(self, cp):
        return None

    def country_profiles(self):
        return []


def _worker(app, settings, tmp_path):
    from dps.ingest.pipeline import Ingestor
    from dps.ingest.store import LocalStore
    from dps.parse import sandbox

    settings.derived_dir = tmp_path / "derived"
    store = LocalStore(settings.storage_dir, tmp_path / "staging", settings.derived_dir)
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)
    page_models: dict = {}
    deps = statement_deps(settings.model_copy(update={"ocr_enabled": False}), app.state.db, Core(), page_models)

    def runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, None, None, db, "1", raw, statement_deps=deps))

    return Worker(settings, app.state.db.session_factory, object(), {}, ingestor, page_models, pack_runner=runner)


# ---------- one pipeline ----------

def test_wp7_every_document_job_runs_in_the_skills_flow():
    assert {spec["flow"] for t, spec in JOB_TYPES.items() if t != "ANALYZE"} == {"SKILL_PACK"}
    for gone in ("dps.flows.intake", "dps.flows.statement", "dps.api.documents", "dps.learning"):
        assert importlib.util.find_spec(gone) is None, gone


def test_wp7_a_statement_job_stores_only_what_a_person_approved(client, app, settings, tmp_path):
    """G21: nothing reaches the values table before review. G22: the figures as printed. G9: Spring hears of it
    from the event feed, not from a call to Core."""
    w = _worker(app, settings, tmp_path)
    job_id = submit(client, app, "EXTRACT_STATEMENT", STATEMENT, ".csv", "text/csv", "st-wp7", "key-st-wp7",
                    asOfDate="2026-03-31").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert res["status"] == "NEEDS_REVIEW" and res["pipeline"]["skillPack"].startswith("statement_lines/")
    lines = {f["name"]: f for f in res["fields"] if f["group"] == "STATEMENT_LINE"}
    assert lines["EBITDA"]["value"]["amount"] == 120 and "TOTAL_DEBT" not in lines  # Spring works totals out
    assert res["statement"]["loanCurrency"] == "INR" and res["document"]["lines"]["notFound"]
    with app.state.db.session_factory() as s:
        assert s.scalars(select(StatementValue)).first() is None
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    blocking = [{"fieldId": f"warning:{w['code']}", "action": "ACKNOWLEDGE", "comment": "checked"}
                for w in res["warnings"] if w["code"] in skills.BLOCKING]
    r = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**SERVICE, "If-Match": str(review["revision"]),
                                                               "X-User": "checker-1"},
                    json={"decisions": [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]
                          + blocking})
    assert r.status_code == 202, r.text
    assert r.json()["remaining"] == 0 and r.json()["status"] == jobs.DONE
    with app.state.db.session_factory() as s:
        stored = {v.key: v for v in s.scalars(select(StatementValue))}
        memory = s.scalars(select(LabelMemory).where(LabelMemory.key == "EBITDA")).first()
    assert stored["EBITDA"].value == 120 and not any(v.derived for v in stored.values())
    # the reviewer's confirmation is remembered in the form the reader looks up (it was stored as written before)
    assert memory is not None and memory.label == "ebitda"
    values = client.post("/api/data/values", json={"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]},
                         headers=h("core", "SERVICE")).json()
    assert values["points"][0]["values"] == {"EBITDA": 120.0}
    events = [e["type"] for e in client.get("/dps/v1/events?after=0", headers=SERVICE).json()["events"]
              if e["jobId"] == job_id]
    assert events[-1] == "dps.extraction.completed"


def test_wp7_an_agreement_job_reads_a_word_file_like_onboard(client, app, settings, tmp_path):
    """G3: one acceptance rule. EXTRACT_AGREEMENT refused Word files that ONBOARD accepted."""
    import io

    from docx import Document as WordDocument

    doc = WordDocument()
    doc.add_paragraph("The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times.")
    buf = io.BytesIO()
    doc.save(buf)
    r = submit(client, app, "EXTRACT_AGREEMENT", buf.getvalue(), ".docx",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "agr-docx", "key-agr-docx")
    assert r.status_code == 202, r.text
    assert _worker(app, settings, tmp_path).run_once()
    res = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    assert any(f["value"].get("threshold") == 1.25 for f in res["fields"] if f["group"] == "FINANCIAL_COVENANT")


def test_wp7_a_statement_in_another_currency_is_not_stored_without_its_rate(client, app, settings, tmp_path):
    w = _worker(app, settings, tmp_path)
    usd = STATEMENT.replace(b"Amounts in INR crore", b"Amounts in USD million")
    job_id = submit(client, app, "EXTRACT_STATEMENT", usd, ".csv", "text/csv", "st-usd", "key-st-usd",
                    asOfDate="2026-03-31").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert "CURRENCY_DIFFERS" in {w["code"] for w in res["warnings"]}
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    r = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**SERVICE, "If-Match": str(review["revision"])},
                    json={"decisions": [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]
                          + [{"fieldId": f"warning:{w['code']}", "action": "ACKNOWLEDGE", "comment": "seen"}
                             for w in res["warnings"] if w["code"] in skills.BLOCKING]})
    assert r.status_code == 409 and r.json()["code"] == "CURRENCY_DIFFERS"
    with app.state.db.session_factory() as s:
        assert s.scalars(select(StatementValue)).first() is None and s.get(Job, job_id).status == jobs.NEEDS_REVIEW


# ---------- G5: readiness says what the workers, the parser and the model are doing ----------

def test_g5_readiness_reports_the_parser_and_the_model(client, app):
    ready = client.get("/health/ready").json()
    assert ready["status"] == "UP" and ready["parser"] == "IN_PROCESS" and ready["model"] == "OFF"
    from dps.api.jobs import _parser_state

    assert _parser_state(os.path.join(os.getcwd(), "no-such-parser.sock")) in ("DOWN", "UNKNOWN")


def test_g5_the_model_state_comes_from_its_recent_calls(app, settings):
    from types import SimpleNamespace

    from dps.api.jobs import _model_state
    from dps.models import AiCall

    request = SimpleNamespace(app=app)
    on = settings.model_copy(update={"llm_enabled": True, "ai_model": "azure/gpt"})
    assert _model_state(request, on) == "IDLE"
    with app.state.db.session_factory() as s:
        for status in ("ERROR", "ERROR", "ERROR"):
            s.add(AiCall(purpose="EXTRACT", user="u", model="m", prompt_version="p", status=status))
        s.commit()
    assert _model_state(request, on) == "DOWN"
    with app.state.db.session_factory() as s:
        s.add(AiCall(purpose="EXTRACT", user="u", model="m", prompt_version="p", status="OK"))
        s.commit()
    assert _model_state(request, on) == "DEGRADED"


# ---------- G24: the event feed under concurrent writers (SQL Server) ----------

@pytest.mark.skipif(not os.environ.get("DPS_TEST_DATABASE_URL", "").startswith("mssql"),
                    reason="needs two concurrent writers: SQL Server (the mssql CI job)")
def test_g24_a_reader_never_passes_an_event_still_being_written(settings):
    from dps.db import Base, Database

    database = Database(settings.database_url)
    Base.metadata.create_all(database.engine)
    slow, fast, reader = (database.session_factory() for _ in range(3))
    try:
        first = feed.emit(slow, "dps.job.accepted", job_id="slow")
        slow.flush()  # the IDENTITY value is taken; the transaction is still open
        feed.emit(fast, "dps.job.accepted", job_id="fast")
        fast.commit()  # a higher seq commits first
        events, cursor = feed.read(reader, first.seq - 1, 10)
        assert events == [] and cursor == first.seq - 1  # never past the seq still being written
        slow.commit()
        reader.rollback()
        events, cursor = feed.read(reader, first.seq - 1, 10)
        assert [e["jobId"] for e in events] == ["slow", "fast"]
    finally:
        for s in (slow, fast, reader):
            s.close()


def test_g24_the_gap_rule_without_a_second_writer(app):
    """The same rule on any database: a recent gap stops the page; a settled one does not."""
    from datetime import timedelta

    from dps.models import EventFeed, utcnow

    with app.state.db.session_factory() as s:
        a = feed.emit(s, "dps.job.accepted", job_id="a")
        s.flush()
        s.add(EventFeed(seq=a.seq + 2, type="dps.job.accepted", tenant_id="default", job_id="c", payload_json="{}"))
        s.commit()
        events, _ = _read(s, a.seq - 1)
        assert events == ["a"]  # seq a+1 may still be committing: stop before it
        events, _ = _read(s, a.seq - 1, now=utcnow() + timedelta(seconds=feed.GAP_SETTLE_SECONDS + 5))
        assert events == ["a", "c"]  # a gap older than the settle time is a rollback: go on


def _read(db, after, now=None):
    rows, cursor = feed.read(db, after, 10, now=now)
    return [e["jobId"] for e in rows], cursor

