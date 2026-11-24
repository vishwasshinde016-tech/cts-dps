"""Improvement plan Group 1 (24 Nov 2026): speed, scale and safety fixes found in the full review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_ai_guard import audits, database, spend  # noqa: F401 - the fixture

from dps.agent.guard import AiBudgetExhausted, AiDailyBudgetExhausted, check_budget, guarded
from dps.settings import Settings

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


# ---------- S11 the day's token cap

def test_the_daily_cap_stops_the_model_for_the_rest_of_the_day(database):  # noqa: F811
    settings = Settings(ai_daily_token_budget=1000)
    spend(database, 400, NOW - timedelta(days=1))  # yesterday does not count
    spend(database, 900, NOW - timedelta(hours=2))
    with database.session_factory() as s:
        check_budget(s, settings, NOW)  # 900 of 1000: still allowed
    spend(database, 100, NOW - timedelta(minutes=5))
    with database.session_factory() as s, pytest.raises(AiDailyBudgetExhausted):
        check_budget(s, settings, NOW)
    assert audits(database, "AI_DAILY_BUDGET_EXHAUSTED") == 1
    with database.session_factory() as s:
        check_budget(s, settings, NOW + timedelta(days=1))  # a new day


def test_the_routes_files_daily_budget_is_enforced_when_no_setting(database):  # noqa: F811
    spend(database, 500, NOW - timedelta(hours=1))
    called = []
    call = guarded(lambda m: called.append(m) or "ok", "m", database.session_factory, Settings(), lambda: NOW,
                   daily=500)
    with pytest.raises(AiBudgetExhausted):
        call([{"role": "user", "content": "x"}])
    assert called == []  # the model was not called


def test_a_spent_day_is_reported_as_its_own_code_and_not_as_an_outage():
    from dps.agent.gateway import AiDailyBudgetSpent, AiMonthlyBudgetSpent

    assert AiDailyBudgetSpent.code == "AI_DAILY_BUDGET_SPENT"
    assert issubclass(AiDailyBudgetSpent, AiMonthlyBudgetSpent)  # handled like a spent month: no breaker failure


# ---------- S3 queue limits for bulk uploads

def test_bulk_uploads_run_more_files_at_once():
    s = Settings()
    assert s.queue_max_running_per_tenant == 10 and s.queue_max_running_per_batch == 6
    assert s.queue_max_running_per_batch < s.queue_max_running_per_tenant  # room left for interactive uploads


# ---------- S5 lookup indexes

def test_jobs_have_lookup_indexes():
    from dps.models import Job

    names = {i.name for i in Job.__table__.indexes}
    assert {"ix_dps_job_document", "ix_dps_job_version", "ix_dps_job_correlation"} <= names


# ---------- S10 a cancelled job stops before its next AI call

def test_the_ai_readers_stop_when_the_job_is_cancelled():
    from dps import jobs
    from dps.skills import load
    from dps.skills.common import batched_proposals

    calls = []
    token = jobs.CANCEL_CHECK.set(lambda: len(calls) >= 1)  # cancelled after the first batch
    try:
        import dps.skills.common as common

        real = common.agent_proposals
        common.agent_proposals = lambda *a, **kw: calls.append(1) or []
        try:
            with pytest.raises(jobs.JobCancelled):
                batched_proposals(load("covenants"), object(), [[{"no": 1}], [{"no": 2}], [{"no": 3}]], notes=[])
        finally:
            common.agent_proposals = real
    finally:
        jobs.CANCEL_CHECK.reset(token)
    assert calls == [1]  # batches 2 and 3 were never sent


def test_a_job_cancelled_while_reading_ends_cancelled_with_no_result(client, app, settings, tmp_path):
    from conftest import h
    from test_phase2_ingest import text_pdf
    from test_phase4_skills import make_worker, submit

    from dps import jobs
    from dps.models import Job, Run

    w = make_worker(app, settings, tmp_path)

    def runner(db, job, model, raw):
        with app.state.db.session_factory() as s:  # the user cancels while the AI reads
            s.get(Job, job.id).cancel_requested = True
            s.commit()
        jobs.stop_if_cancelled()

    w.pack_runner = runner
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["The Borrower shall maintain DSCR of 1.25."]), ".pdf",
               "application/pdf", "cx-1", "key-cancel-001")
    assert w.run_once()
    job = client.get(f"/dps/v1/jobs/{r.json()['jobId']}", headers=h("cts", "SERVICE")).json()
    assert job["status"] == "CANCELLED" and job["errorCode"] == "JOB_CANCELLED"
    with app.state.db.session_factory() as s:
        assert s.query(Run).filter(Run.job_id == job["id"]).count() == 0


# ---------- E1 an event Spring refuses does not block the later ones

def test_a_refused_event_is_recorded_and_skipped(client, app, settings):
    import json

    import httpx
    from test_phase2_ingest import text_pdf
    from test_phase4_skills import submit

    from dps.integrations.spring import SpringClient
    from dps.models import AuditEvent, PushCursor
    from dps.worker import Worker

    received = []

    def handler(req: httpx.Request) -> httpx.Response:
        events = json.loads(req.content)["events"]
        if any(e.get("payload", {}).get("jobType") == "EXTRACT_SANCTION" for e in events):  # Spring refuses it
            return httpx.Response(422)
        received.extend(events)
        return httpx.Response(202)

    core = SpringClient("http://spring", 5, client=httpx.Client(base_url="http://spring",
                                                              transport=httpx.MockTransport(handler)))
    submit(client, app, "EXTRACT_SANCTION", text_pdf(["SANCTION LETTER"]), ".pdf", "application/pdf", "ev-1",
           "key-ev-00001")
    submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["FACILITY AGREEMENT"]), ".pdf", "application/pdf", "ev-2",
           "key-ev-00002")
    settings.events_push_path = "/api/v1/dps-tools/events"
    w = Worker(settings, app.state.db.session_factory, core, {})
    assert w.push_events() == 1  # the agreement's event got through; the sanction's was refused
    assert [e["payload"]["jobType"] for e in received] == ["EXTRACT_AGREEMENT"]
    with app.state.db.session_factory() as s:
        assert s.query(AuditEvent).filter(AuditEvent.action == "EVENT_PUSH_REJECTED").count() == 1
        assert s.get(PushCursor, "spring").seq > 0  # the cursor moved on


def test_a_wrong_token_is_never_skipped(client, app, settings):
    import httpx
    from test_phase2_ingest import text_pdf
    from test_phase4_skills import submit

    from dps.integrations.spring import SpringClient
    from dps.models import PushCursor
    from dps.worker import Worker

    core = SpringClient("http://spring", 5, client=httpx.Client(
        base_url="http://spring", transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["FACILITY AGREEMENT"]), ".pdf", "application/pdf", "ev-3",
           "key-ev-00003")
    settings.events_push_path = "/api/v1/dps-tools/events"
    w = Worker(settings, app.state.db.session_factory, core, {})
    assert w.push_events() == 0
    with app.state.db.session_factory() as s:
        assert s.get(PushCursor, "spring").seq == 0  # waits for the token to be fixed


# ---------- A2 model pins and every prompt hash

def test_the_result_names_every_prompt_that_could_reach_the_model():
    from dps.skills import load
    from dps.skills.runner import _packs_used

    used = _packs_used(load("covenants"))
    assert {"covenants", "schedule_terms", "covenants.parts", "covenants.residual", "classify"} <= set(used)
    assert all(len(v["promptSha"]) == 16 for v in used.values())


def test_a_model_call_records_what_it_was_pinned_to(monkeypatch):
    from dps.agent.gateway import Gateway, Route, Routes

    monkeypatch.setenv("AZURE_API_VERSION", "2025-04-01-preview")
    route = Route("strong", "azure", "azure/gpt-x", max_tokens=1234, timeout_s=90, temperature=None)
    gw = Gateway(Routes({"strong": route}), lambda r: (lambda m: '{"ok": 1}'))
    budget = gw.begin_job(1000)
    gw.call("strong", [{"role": "user", "content": "x"}])
    gw.end_job()
    assert budget.pins["strong"] == {"deployment": "azure/gpt-x", "apiVersion": "2025-04-01-preview",
                                     "maxTokens": 1234, "timeoutSeconds": 90, "temperature": None}


# ---------- T3 the version follows the settings that change results

def test_changing_an_output_setting_changes_the_pipeline_version(tmp_path):
    base = Settings(pipeline_version="auto").pipeline_version
    assert Settings(pipeline_version="auto", review_confidence=0.9).pipeline_version != base
    assert Settings(pipeline_version="auto", queue_max_running_per_tenant=3).pipeline_version == base  # not output
    (tmp_path / "loan_agreement.json").write_text('{"w": [1]}')
    assert Settings(pipeline_version="auto", scorer_dir=str(tmp_path)).pipeline_version != base
    assert Settings(pipeline_version="pinned-7", review_confidence=0.9).pipeline_version == "pinned-7"


# ---------- G3 a fair QA sample

def test_the_qa_sample_differs_between_jobs_for_the_same_field():
    from dps.scoring import route

    picked = 0
    for n in range(2000):
        f = {"id": "fin-dscr-p3", "confidence": 0.99, "status": None}
        route(f, "LOW", job_id=f"job-{n}")
        picked += f["status"] == "NEEDS_REVIEW"
    assert 30 < picked < 95  # about 3 % of jobs, not all or none
    again = {"id": "fin-dscr-p3", "confidence": 0.99, "status": None}
    route(again, "LOW", job_id="job-7")
    first = {"id": "fin-dscr-p3", "confidence": 0.99, "status": None}
    route(first, "LOW", job_id="job-7")
    assert again["status"] == first["status"]  # reproducible for one job


# ---------- N3 a closed statement stores the lines a person confirmed; N4 the period date set in the review

def _auto_statement(client, app, settings, tmp_path, rows, version, key):
    from test_cts_auto import _statement_worker
    from test_flows import xlsx_bytes
    from test_phase4_skills import submit

    w = _statement_worker(app, settings, tmp_path)
    r = submit(client, app, "AUTO", xlsx_bytes(rows), ".xlsx",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", version, key)
    assert r.status_code == 202, r.text
    assert w.run_once()
    return r.json()["jobId"]


def _values(client, keys):
    from conftest import h

    return client.post("/api/data/values", headers=h("cts", "SERVICE"),
                       json={"cpId": "CP-1", "to": "2026-12-31", "keys": keys}).json()


def test_close_stores_only_the_confirmed_lines_of_a_statement(client, app, settings, tmp_path):
    from conftest import h
    from test_cts_auto import ROWS

    svc = h("cts", "SERVICE")
    job_id = _auto_statement(client, app, settings, tmp_path, ROWS, "n3-1", "key-n3-00001")
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=svc).json()
    ebitda = next(f for f in review["fields"] if f["name"] == "EBITDA")
    r = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**svc, "If-Match": str(review["revision"])},
                    json={"decisions": [{"fieldId": ebitda["fieldId"], "action": "CONFIRM"}]})
    assert r.status_code == 202, r.text
    closed = client.post(f"/dps/v1/jobs/{job_id}/close", headers=svc, json={"reason": "figures used in CTS"}).json()
    assert closed["published"] == 1 and "notPublishedReason" not in closed
    got = _values(client, ["EBITDA", "TOTAL_EQUITY", "NET_WORTH"])
    assert got["points"][-1]["values"] == {"EBITDA": 5.0}  # only the confirmed line


def test_close_stores_nothing_while_a_blocking_warning_is_open(client, app, settings, tmp_path):
    from conftest import h
    from test_cts_auto import ROWS

    svc = h("cts", "SERVICE")
    job_id = _auto_statement(client, app, settings, tmp_path, ROWS[1:], "n3-2", "key-n3-00002")  # no period date
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=svc).json()
    assert "PERIOD_DATE_MISSING" in {w["code"] for w in review["warnings"]}
    ebitda = next(f for f in review["fields"] if f["name"] == "EBITDA")
    client.post(f"/dps/v1/jobs/{job_id}/review", headers={**svc, "If-Match": str(review["revision"])},
                json={"decisions": [{"fieldId": ebitda["fieldId"], "action": "CONFIRM"}]})
    closed = client.post(f"/dps/v1/jobs/{job_id}/close", headers=svc, json={"reason": "r"}).json()
    assert closed["published"] == 0 and "PERIOD_DATE_MISSING" in closed["notPublishedReason"]


def test_the_reviewer_sets_the_period_date_and_the_figures_are_stored(client, app, settings, tmp_path):
    from conftest import h
    from test_cts_auto import ROWS

    from dps.models import Document, Job

    svc = h("cts", "SERVICE")
    job_id = _auto_statement(client, app, settings, tmp_path, ROWS[1:], "n4-1", "key-n4-00001")
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=svc).json()
    decisions = [{"fieldId": "document:asOfDate", "action": "CORRECT", "value": {"asOfDate": "2026-03-31"}}]
    decisions += [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]
    decisions += [{"fieldId": w["fieldId"], "action": "ACKNOWLEDGE", "comment": "checked"} for w in review["warnings"]
                  if w["code"] != "PERIOD_DATE_MISSING"]
    r = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**svc, "If-Match": str(review["revision"])},
                    json={"decisions": decisions})
    assert r.status_code == 202 and r.json()["status"] == "DONE", r.text
    with app.state.db.session_factory() as s:
        assert str(s.get(Document, s.get(Job, job_id).document_id).as_of_date) == "2026-03-31"
    assert _values(client, ["EBITDA"])["points"][-1] == {"asOfDate": "2026-03-31", "values": {"EBITDA": 5.0}}


def test_the_period_date_is_refused_on_an_agreement(client, app, settings, tmp_path):
    from conftest import h
    from test_phase2_ingest import text_pdf
    from test_phase4_skills import make_worker, submit

    svc = h("cts", "SERVICE")
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["The Borrower shall maintain a Debt Service Coverage "
                                                            "Ratio of not less than 1.25 times."]),
               ".pdf", "application/pdf", "n4-2", "key-n4-00002")
    assert w.run_once()
    review = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/review", headers=svc).json()
    bad = client.post(f"/dps/v1/jobs/{r.json()['jobId']}/review",
                      headers={**svc, "If-Match": str(review["revision"])},
                      json={"decisions": [{"fieldId": "document:asOfDate", "action": "CORRECT",
                                           "value": {"asOfDate": "2026-03-31"}}]})
    assert bad.status_code == 400


# ---------- V2 test frequencies (golden-set check)

@pytest.mark.parametrize("text,expected", [
    ("shall not be greater than 3.00:1 on each Quarter Date", "QUARTERLY"),
    ("shall not be less than 1.50 times in any quarter", "QUARTERLY"),
    ("as at each financial quarter end", "QUARTERLY"),
    ("not exceeding INR 25 crore in any Financial Year", "YEARLY"),
    ("tested quarterly on a trailing twelve month basis", "QUARTERLY"),
    ("shall at no time be lower than 2.25 times", None),
])
def test_testing_wordings_give_the_frequency(text, expected):
    from dps.extraction.agreement import _frequency

    assert _frequency(text) == expected


def test_a_section_testing_statement_applies_to_its_other_ratios_only():
    from dps.extraction.agreement import Term, _section_frequency

    def term(metric, quote, freq=None):
        return Term(metric, "FINANCIAL", ">=", 1.0, None, freq, 1, quote)

    dscr = term("DSCR", "ARTICLE 7 FINANCIAL COVENANTS 7.1 The DSCR shall not be less than 1.35 times, tested "
                        "quarterly.", "QUARTERLY")
    lev = term("DEBT_TO_EBITDA", "1.35 times, tested quarterly. 7.2 Total Debt to EBITDA shall not be more than "
                                 "3.50 times on each test date.")
    other = term("ICR", "ARTICLE 9 OTHER 9.1 The Interest Coverage Ratio shall not be less than 2.00 times.")
    _section_frequency([dscr, lev, other])
    assert lev.frequency == "QUARTERLY" and any(i.startswith("FREQUENCY_FROM_SECTION") for i in lev.issues)
    assert other.frequency is None  # another article: never guessed


def test_a_calculation_period_is_not_passed_on():
    from dps.extraction.agreement import Term, _section_frequency

    dscr = Term("DSCR", "FINANCIAL", ">=", 1.2, None, "YEARLY", 1,
                "ARTICLE 6 FINANCIAL COVENANTS 6.1 The DSCR shall not fall below 1.20 times for any Financial Year.")
    cur = Term("CURRENT_RATIO", "FINANCIAL", ">=", 1.1, None, None, 1,
               "below 1.20 times for any Financial Year. 6.2 The Current Ratio shall not be less than 1.10 times.")
    _section_frequency([dscr, cur])
    assert cur.frequency is None
