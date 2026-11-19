"""Full review, 19 Nov 2026 (docs/cts-dps/full-review): one test per confirmed defect, so each fix stays fixed.

The IDs (F1 ... G34) are the finding IDs in FIX-PLAN.md. Every test states the behaviour the review asked for.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from test_flows import add_document, database, doc_status, intake, terms  # noqa: F401 - fixture
from test_phase5_review import SERVICE, _run_certificate

from dps.models import Run
from dps.parse.engine import Page

SPRINGING = [Page(1, "7.1 Fixed Charge Coverage Ratio\nIf Excess Availability is less than USD 10,000,000 on any day, "
                     "the Borrower shall maintain a Fixed Charge Coverage Ratio of at least 1.10:1.\n", "TEXT", 0.95)]
CLEAN = [Page(1, "4.1 Debt Service Coverage Ratio\nThe Borrower shall maintain a DSCR of not less than 1.25 times.\n"
                 "4.2 Interest Coverage Ratio\nThe Borrower shall maintain an ICR of not less than 2.00 times.\n",
              "TEXT", 0.95)]


# ---------- WP1: nothing is applied without a person ----------

def test_f5_a_clean_agreement_is_never_published_without_a_person(database):  # noqa: F811
    doc_id = add_document(database)
    flow = intake(database, CLEAN)
    flow.kickoff(inputs={"job_id": "j-f5", "document_id": doc_id})
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert doc_status(database, doc_id) == "NEEDS_REVIEW"
    stored = terms(database, doc_id)
    assert stored and all(t.status != "PUBLISHED" for t in stored.values())


def test_f5_a_dps_formula_is_marked_as_a_template(database):  # noqa: F811
    doc_id = add_document(database)
    intake(database, SPRINGING).kickoff(inputs={"job_id": "j-f5b", "document_id": doc_id})
    [term] = terms(database, doc_id).values()
    assert "FORMULA_TEMPLATE" in term.issues_json


def test_f12_rules_only_says_the_agent_did_not_read():
    from dps.skills import load
    from dps.skills.runner import _extract

    class Job:
        id, correlation_id, job_type, document_version_id, tenant_id = "j", None, "ONBOARD", "v", "default"

    pages = [{"no": 1, "text": CLEAN[0].text, "quality": 0.95, "sourceMethod": "TEXT"}]
    _, warnings, _ = _extract(load("covenants"), Job(), {"pages": pages}, {}, None, None, None, "1")
    assert "AI_NOT_USED" in {w["code"] for w in warnings}


def test_g29_a_blocking_warning_needs_an_acknowledgement(client, app, settings, tmp_path):
    job_id = _run_certificate(client, app, settings, tmp_path, version="cv-g29", key="key-g29-cert")
    with app.state.db.session_factory() as db:
        run = db.scalars(select(Run).where(Run.job_id == job_id).order_by(Run.run_no.desc())).first()
        result = json.loads(run.result_json)
        result.setdefault("warnings", []).append({"code": "ENTITY_MISMATCH", "message": "another borrower"})
        run.result_json = json.dumps(result)
        db.commit()
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    confirm = {"decisions": [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]}
    r = client.post(f"/dps/v1/jobs/{job_id}/review", json=confirm,
                    headers={**SERVICE, "If-Match": str(review["revision"]), "X-User": "checker"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "NEEDS_REVIEW"  # every field confirmed, the mismatch still open
    bad = client.post(f"/dps/v1/jobs/{job_id}/review",
                      json={"decisions": [{"fieldId": "warning:ENTITY_MISMATCH", "action": "ACKNOWLEDGE"}]},
                      headers={**SERVICE, "If-Match": str(r.json()["revision"])})
    assert bad.status_code == 400  # a reason is required
    ok = client.post(f"/dps/v1/jobs/{job_id}/review",
                     json={"decisions": [{"fieldId": "warning:ENTITY_MISMATCH", "action": "ACKNOWLEDGE",
                                          "comment": "group company, same obligor"}]},
                     headers={**SERVICE, "If-Match": str(r.json()["revision"]), "X-User": "checker"})
    assert ok.status_code == 202 and ok.json()["status"] == "DONE"
    final = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert final["document"]["route"] == "REVIEWED"
    warning = next(w for w in final["warnings"] if w["code"] == "ENTITY_MISMATCH")
    assert warning["acknowledged"] == {"by": "checker", "comment": "group company, same obligor"}


def test_g21_a_clean_statement_waits_for_review_before_core_sees_it(database):  # noqa: F811
    from conftest import review_done
    from test_flows import GOOD_ROWS, statement, values, xlsx_bytes

    flow, doc_id = statement(database, xlsx_bytes(GOOD_ROWS), "clean.xlsx", reviewed=False)
    assert flow.state.outcome == "NEEDS_REVIEW" and not flow.state.issues
    assert values(database) == {}  # nothing reached Core
    assert review_done(flow).state.outcome == "REPORTED"
    assert values(database)["EBITDA"].value == 5.0


# ---------- WP2: jobs, queue and events ----------

def _queued_job(database, **kw):  # noqa: F811
    from dps.models import Job, utcnow

    doc = add_document(database)
    with database.session_factory() as s:
        j = Job(document_id=doc, flow="AGREEMENT_INTAKE", job_type="EXTRACT_AGREEMENT", tenant_id="default",
                priority=1, due_at=utcnow(), status="QUEUED", pool="STANDARD", attempts=0, crash_count=0,
                cancel_requested=False, **kw)
        s.add(j)
        s.commit()
        return j.id


def test_g6_a_job_whose_worker_died_on_its_last_attempt_ends_dead(database):  # noqa: F811
    from datetime import timedelta

    from dps import jobs
    from dps.models import EventFeed, Job, utcnow

    job_id = _queued_job(database)
    with database.session_factory() as s:
        for n in range(3):
            j = jobs.claim(s, f"w{n}", "STANDARD", 120, 3, poison_crashes=99)
            assert j is not None
            j.locked_until = utcnow() - timedelta(seconds=1)  # the worker died: its lease ran out
            s.commit()
        assert jobs.claim(s, "w9", "STANDARD", 120, 3, poison_crashes=99) is None
        assert s.get(Job, job_id).status == jobs.RUNNING  # before the fix it stayed here for ever
        assert jobs.reap(s, 3) == 1
        j = s.get(Job, job_id)
        assert (j.status, j.error_code) == (jobs.DEAD, "LEASE_EXPIRED_EXHAUSTED")
        events = [e.type for e in s.scalars(select(EventFeed).where(EventFeed.job_id == job_id))]
        assert events == ["dps.job.dead"]
        assert jobs.reap(s, 3) == 0


def test_g7_a_job_cancelled_while_running_is_not_published(database):  # noqa: F811
    from dps import jobs
    from dps.models import Job

    job_id = _queued_job(database)
    with database.session_factory() as s:
        j = jobs.claim(s, "w1", "STANDARD", 120, 3)
        jobs.request_cancel(s, j, "rm-anil")
        token = jobs.worker_attempt.set((job_id, "w1", j.attempts))
        try:
            jobs.finish(s, j, jobs.DONE)  # the flow ended normally after the cancel
        finally:
            jobs.worker_attempt.reset(token)
        assert (s.get(Job, job_id).status, s.get(Job, job_id).error_code) == (jobs.CANCELLED, "JOB_CANCELLED")


def test_g8_review_and_mapping_are_different_batch_outcomes():
    from dps import jobs
    from dps.models import Job

    assert jobs.item_outcome(Job(status=jobs.NEEDS_REVIEW, error_code=None)) == "PENDING_REVIEW"
    assert jobs.item_outcome(Job(status=jobs.NEEDS_REVIEW, error_code="MAPPING_REVIEW")) == "PENDING_MAPPING"


def test_g24_the_feed_never_passes_a_recent_gap(database):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from dps import feed
    from dps.models import EventFeed

    now = datetime.now(UTC)
    with database.session_factory() as s:
        for seq in (1, 2, 4):  # 3 is still being written by another transaction
            s.add(EventFeed(seq=seq, type="dps.job.accepted", tenant_id="default", payload_json="{}",
                            occurred_at=now))
        s.commit()
        events, cursor = feed.read(s, 0, 100, now=now)
        assert [e["seq"] for e in events] == [1, 2] and cursor == 2
        later = now + timedelta(seconds=feed.GAP_SETTLE_SECONDS + 1)  # an old gap is a rollback: read past it
        events, cursor = feed.read(s, 2, 100, now=later)
        assert [e["seq"] for e in events] == [4] and cursor == 4


def test_g16_each_worker_takes_its_share_of_the_deployment_rate():
    from dps.agent.gateway import Routes

    assert Routes.load(None, "gpt", share=4).routes["strong"].rpm == 75
    assert Routes.load(None, "gpt").routes["strong"].rpm == 300


def test_g5_readiness_reports_the_workers(client):
    body = client.get("/health/ready").json()
    assert body["workers"] in ("UP", "DOWN", "UNKNOWN")


# ---------- WP3: versions and custody ----------

def test_g13_the_pipeline_version_changes_with_what_decides_the_result(tmp_path):
    from dps.settings import Settings
    from dps.versioning import compute

    auto = Settings().pipeline_version
    assert auto.startswith("0.") and "+" in auto  # computed, not the old fixed "1"
    assert Settings(pipeline_version="pinned-7").pipeline_version == "pinned-7"
    routes = tmp_path / "routes.yaml"
    routes.write_text("routes: {strong: {deployment: gpt-a}}", encoding="utf-8")
    first = compute(str(routes), "")
    routes.write_text("routes: {strong: {deployment: gpt-b}}", encoding="utf-8")
    compute.cache_clear()
    assert compute(str(routes), "") != first  # a model-route change is a new version


def test_g17_the_model_answer_cache_is_off_and_expires(tmp_path):
    from datetime import timedelta

    from dps.agent.guard import guarded, purge_cache
    from dps.db import Base, Database
    from dps.models import AiCache, utcnow
    from dps.settings import Settings

    assert Settings().ai_cache is False
    db = Database(f"sqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(db.engine)
    s = Settings(ai_cache=True, ai_cache_days=30)
    calls = []
    call = guarded(lambda m: calls.append(1) or "answer", "gpt", db.session_factory, s)
    msgs = [{"role": "user", "content": "x"}]
    assert call(msgs) == "answer" and call(msgs) == "answer" and len(calls) == 1  # cached
    with db.session_factory() as session:
        for row in session.query(AiCache):
            row.created_at = utcnow() - timedelta(days=31)
        session.commit()
    assert call(msgs) == "answer" and len(calls) == 2  # expired: asked again
    with db.session_factory() as session:
        for row in session.query(AiCache):
            row.created_at = utcnow() - timedelta(days=31)
        session.commit()
        assert purge_cache(session, 30) == 1


def test_g15_g20_file_flags_and_pack_versions_reach_the_result():
    from dps.skills import load
    from dps.skills.runner import run

    class Job:
        id, correlation_id, job_type, document_version_id, tenant_id, sha256 = "j", None, "ONBOARD", "v", "d", "s"
        document_id = "doc"
        request_json, rollout = "{}", "NORMAL"

    pages = [{"no": 1, "text": CLEAN[0].text, "quality": 0.95, "sourceMethod": "TEXT"}]
    result = run(load("covenants"), Job(), {"pages": pages, "flags": ["TRACKED_CHANGES", "IRRELEVANT"]})
    flags = next(w for w in result["warnings"] if w["code"] == "FILE_FLAGS")
    assert "TRACKED_CHANGES" in flags["message"] and "IRRELEVANT" not in flags["message"]
    packs = result["pipeline"]["packs"]
    assert packs["covenants"]["version"] and len(packs["covenants"]["promptSha"]) == 16
    assert "schedule_terms" in packs
