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
