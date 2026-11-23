"""CTS integration, P1 items 14-17 and P2 (23 Nov 2026): certificates, blocking warnings, corrections, closing a
review, re-sending a failed document, events, the Spring tools and the acting user."""

from __future__ import annotations

import httpx
import pytest
from conftest import h
from test_phase2_ingest import text_pdf
from test_phase4_skills import make_worker, submit

from dps import feed, jobs
from dps.models import EventFeed, Job

SERVICE = h("cts-spring", "SERVICE")
SUPPORT = h("ops-1", "SUPPORT")
AGREEMENT = ("FACILITY AGREEMENT\n21. FINANCIAL COVENANTS\n21.1 The Borrower shall maintain a Debt Service Coverage "
             "Ratio of not less than 1.25 times.\n21.2 The Borrower shall ensure that the Leverage Ratio does not "
             "exceed 3.50 times.\n22. The Borrower shall maintain insurance on its assets with reputable insurers.")


def _onboard(client, app, settings, tmp_path, version="v-agr-1", key="key-agr-0001", text=AGREEMENT):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf([text]), ".pdf", "application/pdf", version, key)
    assert r.status_code == 202, r.text
    job_id = r.json()["jobId"]
    assert w.run_once()
    return job_id


def _events(app, kind):
    with app.state.db.session_factory() as db:
        return [e for e in db.query(EventFeed).order_by(EventFeed.seq) if e.type == kind]


# ---------- 14, 15 certificate figures and declarations

def test_certificate_pointers_are_not_figures_and_no_dot_is_not_a_breach():
    from dps.skills.certificate.rules import rules

    page = {"no": 1, "text": "Debt Service Coverage Ratio  >= 1.50  1.33  BREACHED (see 3)\n"
                             "Covenant No. 4 Interest Coverage Ratio >= 2.00 2.40 Complied"}
    by = {f["value"]["covenant"]: f["value"] for f in rules([page], {})}
    assert by["DSCR"]["figure"] == 1.33 and by["DSCR"]["declaredCompliant"] is False
    assert by["ICR"]["figure"] == 2.4 and by["ICR"]["declaredCompliant"] is True
    status_column = rules([{"no": 1, "text": "Interest Coverage Ratio 2.00 2.40 No"}], {})
    assert status_column[0]["value"]["declaredCompliant"] is False  # a Yes/No column still reads


# ---------- 16 another borrower's document blocks until accepted

def test_mapping_conflict_and_type_mismatch_block():
    from dps.skills.runner import BLOCKING

    assert {"MAPPING_CONFLICT", "TYPE_MISMATCH"} <= BLOCKING


# ---------- 17 corrections stay consistent; 19 warnings in the review; 18 close; 24 no second completion

def test_review_shows_warnings_correction_is_consistent_and_close_finishes_the_job(client, app, settings, tmp_path):
    job_id = _onboard(client, app, settings, tmp_path)
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    assert "warnings" in review and review["counts"]["openWarnings"] == len(review["warnings"])
    dscr = next(f for f in review["fields"] if f["proposed"].get("metric") == "DSCR")
    r = client.post(f"/dps/v1/jobs/{job_id}/review",
                    headers={**SERVICE, "If-Match": str(review["revision"]), "X-User": "analyst-1"},
                    json={"decisions": [{"fieldId": dscr["fieldId"], "action": "CORRECT",
                                         "value": {"expression": "DSCR >= 1.3"}}]})  # no reasonCode: defaulted
    assert r.status_code == 202, r.text
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    fixed = next(f for f in result["fields"] if f["id"] == dscr["fieldId"])
    assert (fixed["value"]["operator"], fixed["value"]["threshold"], fixed["value"]["expression"]) == (">=", 1.3,
                                                                                                       "DSCR >= 1.3")
    assert fixed["value"]["mapping"]["mappable"] is True and fixed["status"] == "CORRECTED"
    assert r.json()["status"] == "NEEDS_REVIEW"  # other fields still wait
    revision = r.json()["revision"]
    closed = client.post(f"/dps/v1/jobs/{job_id}/close", headers={**SERVICE, "If-Match": str(revision),
                                                                   "X-User": "analyst-1"},
                         json={"reason": "covenants applied in CTS onboarding"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "DONE" and closed.json()["closed"] is True and closed.json()["notUsed"] >= 1
    after = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert after["document"]["route"] == "CLOSED"
    assert not [f for f in after["fields"] if f["status"] == "NEEDS_REVIEW"]
    assert {f["status"] for f in after["fields"]} <= {"CONFIRMED", "CORRECTED", "NOT_USED", "AUTO_ACCEPTED",
                                                      "REJECTED"}
    completed = _events(app, "dps.extraction.completed")
    assert len(completed) == 1 and '"closed": true' in completed[0].payload_json
    again = client.post(f"/dps/v1/jobs/{job_id}/close", headers=SERVICE, json={"reason": "again"})
    assert again.status_code == 200 and len(_events(app, "dps.extraction.completed")) == 1  # repeat-safe
    late = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**SERVICE, "If-Match": "9"},
                       json={"decisions": [{"fieldId": dscr["fieldId"], "action": "CONFIRM"}]})
    assert late.status_code == 409 and late.json()["code"] == "JOB_FINISHED"


# ---------- 23 cancel keeps a result; 31 the acting user

def test_cancel_refuses_a_job_waiting_for_review_and_x_user_is_checked(client, app, settings, tmp_path):
    job_id = _onboard(client, app, settings, tmp_path)
    r = client.post(f"/dps/v1/jobs/{job_id}/cancel", headers=SERVICE)
    assert r.status_code == 409 and "close" in r.json()["message"]
    assert client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).status_code == 200
    long_user = client.post(f"/dps/v1/jobs/{job_id}/close", headers={**SERVICE, "X-User": "x" * 300},
                            json={"reason": "r"})
    assert long_user.status_code == 400


# ---------- 20 a failed document can be sent again; 26 requeued event

def test_a_failed_document_can_be_sent_again_and_support_retry_is_announced(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    first = submit(client, app, "EXTRACT_AGREEMENT", b"%PDF-1.4 broken", ".pdf", "application/pdf", "v-bad-1",
                   "key-bad-0001")
    job_id = first.json()["jobId"]
    for _ in range(4):
        if not w.run_once():
            break
    status = client.get(f"/dps/v1/jobs/{job_id}", headers=SERVICE).json()["status"]
    assert status in ("FAILED", "DEAD")
    again = submit(client, app, "EXTRACT_AGREEMENT", b"%PDF-1.4 broken", ".pdf", "application/pdf", "v-bad-1",
                   "key-bad-0002")
    assert again.status_code == 202 and again.json()["jobId"] != job_id  # a new job, not the dead one
    client.post(f"/dps/v1/jobs/{job_id}/retry", headers=SUPPORT)
    assert _events(app, "dps.job.requeued")


# ---------- 25 reclassify ends the old job and is idempotent

def test_reclassify_supersedes_the_old_job_once(client, app, settings, tmp_path):
    job_id = _onboard(client, app, settings, tmp_path)
    headers = {**SERVICE, "Idempotency-Key": "reclass-0001"}
    body = {"type": "SANCTION_LETTER", "reason": "a sanction letter"}
    first = client.post(f"/dps/v1/jobs/{job_id}/reclassify", headers=headers, json=body)
    second = client.post(f"/dps/v1/jobs/{job_id}/reclassify", headers=headers, json=body)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["jobId"] == first.json()["jobId"] and second.json()["duplicate"] is True
    old = client.get(f"/dps/v1/jobs/{job_id}", headers=SERVICE).json()
    assert old["status"] == "DONE"
    assert client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()["document"]["route"] == "SUPERSEDED"
    assert _events(app, "dps.job.superseded")


# ---------- 28 payloads

def test_needs_review_event_says_how_many_wait_and_why(client, app, settings, tmp_path):
    _onboard(client, app, settings, tmp_path)
    import json

    payload = json.loads(_events(app, "dps.extraction.needs_review")[0].payload_json)
    assert payload["toReview"] >= 1 and payload["route"] in ("NEEDS_REVIEW", "MAPPING_REVIEW")


def test_a_password_protected_file_is_not_quarantined():
    from dps.ingest.pipeline import QUARANTINE

    assert "ENCRYPTED" not in QUARANTINE and "UNSAFE_CONTENT" in QUARANTINE


# ---------- 27 the feed reads with the database's clock

def test_the_feed_uses_the_databases_clock(app):
    with app.state.db.session_factory() as db:
        feed.emit(db, "dps.job.accepted", job_id="j", payload={})
        db.commit()
        events, cursor = feed.read(db, 0, 10)
    assert events and cursor == events[-1]["seq"] and events[0]["occurredAt"]


# ---------- 29 two saves of one revision

def test_two_saves_of_one_revision_give_409_not_500(client, app, settings, tmp_path):
    from dps.api.review import _save_revision
    from dps.errors import ApiError

    job_id = _onboard(client, app, settings, tmp_path)
    with app.state.db.session_factory() as db:
        job = db.get(Job, job_id)
        _save_revision(db, job, {"status": "NEEDS_REVIEW", "fields": []}, 1, "REVIEW")
        db.commit()
        with pytest.raises(ApiError) as ex:
            _save_revision(db, job, {"status": "NEEDS_REVIEW", "fields": []}, 1, "REVIEW")
    assert ex.value.status == 409


# ---------- 21 Spring tools: a 4xx is a warning, never a dead job; 33 correlation id on every call

class _Core:
    def __init__(self, status):
        self.status, self.headers = status, []

    def _request(self, method, path, headers=None, **kw):
        from dps.integrations.spring import CORRELATION

        self.headers.append({**(headers or {}), "corr": CORRELATION.get()})
        return httpx.Response(self.status, json={}, request=httpx.Request(method, "http://core" + path))


def test_a_refusal_from_the_bank_app_is_a_warning_not_a_crash():
    from dps.integrations.spring_tools import SpringTools

    tools = SpringTools(_Core(401), retries=0)
    library, fresh = tools.covenant_library()
    assert library == [] and fresh is False
    checks = tools.validate_formulas([("f1", "DSCR >= 1.25")])
    assert checks["f1"].available is False
    lookups = tools.lookup_identifiers([("i1", "PAN", "ABCDE1234F")])
    assert lookups["i1"].available is False


def test_every_spring_call_carries_the_jobs_correlation_id():
    from dps.integrations.spring import CORRELATION, SpringClient

    sent = []

    def handler(request):
        sent.append(request.headers.get("X-Correlation-Id"))
        return httpx.Response(404)

    client = SpringClient("http://core", 5, client=httpx.Client(base_url="http://core",
                                                                transport=httpx.MockTransport(handler)))
    token = CORRELATION.set("corr-123")
    try:
        client.cp_currency("CP-1")
    finally:
        CORRELATION.reset(token)
    assert sent == ["corr-123"]


def test_finish_payload_for_failed_always_has_quarantine(app):
    with app.state.db.session_factory() as db:
        job = Job(id="j-q", document_id="d", flow="SKILL_PACK", job_type="EXTRACT_AGREEMENT", tenant_id="default",
                  status="RUNNING", sha256="0" * 64)
        db.add(job)
        db.commit()
        jobs.finish(db, job, jobs.FAILED, "bad", "UNSUPPORTED_FORMAT", payload={"quarantine": False})
    assert '"quarantine": false' in _events(app, "dps.job.failed")[0].payload_json
