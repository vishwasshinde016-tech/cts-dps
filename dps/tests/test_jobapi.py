"""CTS job API, Phase 1 (contracts/dps-jobs-api.yaml): every rule in the Phase 1 tab has a test here."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import jsonschema
import pytest
import yaml
from conftest import h
from referencing import Registry, Resource

from dps import jobs
from dps.api.jobs import SCHEMA_NAMES
from dps.models import Batch, Document, EventFeed, ExtractedTerm, Job, JobAttempt, StatementValue, utcnow

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
PACKAGED = ROOT / "dps" / "src" / "dps" / "contract"
SERVICE = h("cts-spring", "SERVICE")
SUPPORT = h("support-1", "SUPPORT")


# ---------- contract helpers ----------

def _registry() -> Registry:
    resources = []
    for path in (CONTRACTS / "schemas").glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        resources += [(schema["$id"], resource), (f"schemas/{path.name}", resource)]
    return Registry().with_resources(resources)


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / "schemas" / f"{name}.json").read_text(encoding="utf-8"))


def check(name: str, body) -> None:
    jsonschema.Draft202012Validator(_schema(name), registry=_registry()).validate(body)


def openapi() -> dict:
    return yaml.safe_load((CONTRACTS / "dps-jobs-api.yaml").read_text(encoding="utf-8"))


def check_component(component: str, body) -> None:
    spec = openapi()
    schema = {**spec["components"]["schemas"][component], "components": spec["components"]}
    jsonschema.Draft202012Validator(schema, registry=_registry()).validate(body)


# ---------- request helpers ----------

def stored(app, data: bytes = b"%PDF-1.4 agreement", ext: str = ".pdf") -> tuple[str, str]:
    digest, key = app.state.store.put(data, ext)
    return key, digest


def request_body(app, *, job_type: str = "EXTRACT_AGREEMENT", version: str = "dv-1", data: bytes | None = None,
                 priority: str = "INTERACTIVE", tenant: str = "default", **context) -> dict:
    statement = job_type == "EXTRACT_STATEMENT"
    key, digest = stored(app, data or (b"a,b\n1,2\n" if statement else b"%PDF-1.4 " + version.encode()),
                         ".csv" if statement else ".pdf")
    ctx = {"tenantId": tenant, "cpIds": ["CP-1"], "requestedBy": "rm-anil", **context}
    if statement:
        ctx.setdefault("asOfDate", "2026-03-31")
    return {"jobType": job_type, "priority": priority, "context": ctx,
            "document": {"versionId": version, "objectKey": key, "sha256": digest,
                         "fileName": "f.csv" if statement else "f.pdf",
                         "mediaType": "text/csv" if statement else "application/pdf", "sizeBytes": 20}}


def submit(client, body: dict, key: str = "key-00000001", headers: dict | None = None):
    return client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": key, **(headers or {})})


# ---------- contract files ----------

def test_packaged_contract_matches_the_published_one():
    for folder in ("schemas", "catalogues"):
        for path in (CONTRACTS / folder).iterdir():
            assert (PACKAGED / folder / path.name).read_bytes() == path.read_bytes(), path.name


def test_schemas_and_catalogues_are_valid():
    for path in (CONTRACTS / "schemas").glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    errors = yaml.safe_load((CONTRACTS / "catalogues" / "errors.yaml").read_text(encoding="utf-8"))["errors"]
    codes = [e["code"] for e in errors]
    assert len(codes) == len(set(codes)), "duplicate error code"
    assert all(e["class"] in {"REQUEST", "SECURITY", "CONTENT", "QUALITY", "TRANSIENT", "DEGRADED", "RESOURCE",
                              "INTERNAL"} for e in errors)
    events = yaml.safe_load((CONTRACTS / "catalogues" / "events.yaml").read_text(encoding="utf-8"))["events"]
    assert {e["type"] for e in events} == set(_schema("event")["properties"]["type"]["enum"])


def test_openapi_lists_30_dps_endpoints_and_2_compatibility_ones():
    spec = openapi()
    operations = [(path, method) for path, item in spec["paths"].items() for method in item]
    assert len([o for o in operations if not o[0].startswith("/api/")]) == 30
    assert len([o for o in operations if o[0].startswith("/api/")]) == 2
    assert all("x-phase" in spec["paths"][p][m] for p, m in operations)


def test_every_error_code_the_api_returns_is_in_the_catalogue():
    import re
    codes = {e["code"] for e in yaml.safe_load((CONTRACTS / "catalogues" / "errors.yaml")
                                               .read_text(encoding="utf-8"))["errors"]}
    source = "".join((ROOT / "dps" / "src" / "dps" / name).read_text(encoding="utf-8")
                     for name in ("api/jobs.py", "api/evidence.py", "api/review.py"))
    used = set(re.findall(r'ApiError\(\d+, "([A-Z_]+)"', source)) | set(re.findall(r'"([A-Z_]+)", f?"Same', source))
    used.discard("DUPLICATE")  # a batch item outcome, not an error
    assert used <= codes, used - codes


# ---------- submit ----------

def test_submit_queues_a_job_and_emits_accepted(client, app):
    r = submit(client, request_body(app), headers={"X-Correlation-Id": "corr-1"})
    assert r.status_code == 202, r.text
    body = r.json()
    check_component("JobAccepted", body)
    assert r.headers["Location"] == f"/dps/v1/jobs/{body['jobId']}" and body["duplicate"] is False
    with app.state.db.session_factory() as s:
        job = s.get(Job, body["jobId"])
        assert (job.job_type, job.flow, job.priority, job.correlation_id) == (
            "EXTRACT_AGREEMENT", "AGREEMENT_INTAKE", 1, "corr-1")
        doc = s.get(Document, job.document_id)
        assert doc.external_version_id == "dv-1" and doc.active_cp_ids() == ["CP-1"]
        assert [e.type for e in s.query(EventFeed)] == ["dps.job.accepted"]
    status = client.get(f"/dps/v1/jobs/{body['jobId']}", headers=SERVICE).json()
    check("job-status", status)
    assert status["jobType"] == "EXTRACT_AGREEMENT" and status["priority"] == "INTERACTIVE"


def test_same_key_same_body_returns_the_first_job(client, app):
    body = request_body(app)
    first = submit(client, body).json()
    again = submit(client, body)
    assert again.status_code == 200 and again.json()["jobId"] == first["jobId"] and again.json()["duplicate"]


def test_same_key_different_body_is_refused(client, app):
    submit(client, request_body(app, version="dv-1"))
    r = submit(client, request_body(app, version="dv-2"))
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_same_input_under_two_keys_is_processed_once(client, app):
    body = request_body(app)
    first = submit(client, body, key="key-aaaaaaaa").json()
    second = submit(client, body, key="key-bbbbbbbb")
    assert second.status_code == 200 and second.json()["jobId"] == first["jobId"]
    with app.state.db.session_factory() as s:
        assert s.query(Job).count() == 1


@pytest.mark.parametrize(("change", "status", "code"), [
    ({"jobType": "REPROCESS"}, 422, "UNSUPPORTED_JOB_TYPE"),
    ({"tenant": "other"}, 403, "TENANT_MISMATCH"),
    ({"sha256": "0" * 64}, 400, "INTEGRITY_MISMATCH"),
    ({"objectKey": "../../etc/passwd"}, 400, "INVALID_INPUT"),
    ({"objectKey": "ab/missing.pdf"}, 400, "INVALID_INPUT"),
    ({"mediaType": "text/csv"}, 400, "UNSUPPORTED_FORMAT"),
    ({"versionId": None}, 400, "INVALID_INPUT"),
])
def test_submit_refuses_bad_requests(client, app, change, status, code):
    body = request_body(app)
    if "jobType" in change:
        body["jobType"] = change["jobType"]
    elif "tenant" in change:
        body["context"]["tenantId"] = change["tenant"]
    else:
        body["document"].update(change)
    r = submit(client, body)
    assert r.status_code == status and r.json()["code"] == code, r.text
    assert set(r.json()) >= {"code", "message", "details", "timestamp"}


def test_submit_needs_a_key_a_service_role_and_a_valid_body(client, app):
    body = request_body(app)
    r = client.post("/dps/v1/jobs", json=body, headers=SERVICE)
    assert r.status_code == 400 and r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = client.post("/dps/v1/jobs", json=body, headers={**h(), "Idempotency-Key": "key-00000001"})
    assert r.status_code == 403
    r = submit(client, {**body, "unknown": 1})
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_FAILED"


def test_statement_needs_a_period_end(client, app):
    body = request_body(app, job_type="EXTRACT_STATEMENT")
    body["context"]["asOfDate"] = None
    r = submit(client, body)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_INPUT"


def test_queue_full_answers_429(client, app, settings):
    settings.queue_max_queued_per_tenant = 1
    assert submit(client, request_body(app, version="dv-1"), key="key-00000001").status_code == 202
    r = submit(client, request_body(app, version="dv-2"), key="key-00000002")
    assert r.status_code == 429 and r.json()["code"] == "QUEUE_FULL"


# ---------- result ----------

def test_result_waits_then_matches_the_contract(client, app):
    job_id = submit(client, request_body(app)).json()["jobId"]
    r = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE)
    assert r.status_code == 404 and r.json()["code"] == "RESULT_NOT_READY"
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        s.add(ExtractedTerm(document_id=job.document_id, metric_raw="DSCR", covenant_master_id="m1",
                            expression="DSCR >= 1.25", operator=">=", threshold=1.25, frequency="QUARTERLY",
                            category="FINANCIAL", confidence=0.95, status="PUBLISHED", page=3,
                            quote="not less than 1.25"))
        s.add(ExtractedTerm(document_id=job.document_id, metric_raw="LEVERAGE", category="CANDIDATE",
                            confidence=0.4, status="NEEDS_REVIEW", page=9, quote="shall not exceed",
                            issues_json='["LOW_CONFIDENCE: check this"]'))
        jobs.finish(s, job, jobs.DONE)
    r = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE)
    assert r.status_code == 200 and r.headers["ETag"]
    result = r.json()
    check("result", result)
    assert result["document"]["route"] == "NEEDS_REVIEW"
    assert [f["status"] for f in result["fields"]] == ["AUTO_ACCEPTED", "NEEDS_REVIEW"]
    assert result["fields"][1]["issues"] == [{"code": "LOW_CONFIDENCE", "message": "check this"}]
    again = client.get(f"/dps/v1/jobs/{job_id}/result", headers={**SERVICE, "If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


def test_statement_result_lists_values_with_cells(client, app):
    job_id = submit(client, request_body(app, job_type="EXTRACT_STATEMENT")).json()["jobId"]
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        s.add(StatementValue(cp_id="CP-1", key="EBITDA", value=50.0, unit="INR_CR", as_of_date=date(2026, 3, 31),
                             source_type="DOCUMENT", document_id=job.document_id, cell="BS!C14"))
        jobs.finish(s, job, jobs.DONE)
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    check("result", result)
    assert result["fields"][0]["source"]["cellRef"] == "BS!C14" and result["fields"][0]["confidence"] is None


def test_statement_lines_not_found_are_listed_never_dropped(client, app):
    from dps.catalog import lines_not_found

    job_id = submit(client, request_body(app, job_type="EXTRACT_STATEMENT")).json()["jobId"]
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        s.add(StatementValue(cp_id="CP-1", key="EBITDA", value=50.0, unit="INR_CR", as_of_date=date(2026, 3, 31),
                             source_type="DOCUMENT", document_id=job.document_id, cell="BS!C14"))
        jobs.record_step(s, job, "coverage", lines=lines_not_found(["EBITDA", "TOTAL_DEBT", "NET_WORTH"]))
        jobs.finish(s, job, jobs.DONE)
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    check("result", result)
    lines = result["document"]["lines"]
    assert lines["found"] == 3 and "CAPEX" in lines["notFound"] and "CFO" in lines["notFound"]
    assert not any(k.endswith(("_TTM", "_ANN")) for k in lines["notFound"])
    warning = next(w for w in result["warnings"] if w["code"] == "LINES_NOT_FOUND")
    assert "CAPEX" in warning["message"] and result["document"]["route"] == "AUTO_ACCEPTED"  # visible, not blocking


# ---------- cancel ----------

def test_cancel_a_waiting_job_at_once_and_a_finished_one_never(client, app):
    job_id = submit(client, request_body(app)).json()["jobId"]
    r = client.post(f"/dps/v1/jobs/{job_id}/cancel", headers={**SERVICE, "X-User": "rm-anil"})
    assert r.status_code == 202 and r.json()["status"] == "CANCELLED"
    assert client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).status_code == 410
    r = client.post(f"/dps/v1/jobs/{job_id}/cancel", headers=SERVICE)
    assert r.status_code == 409 and r.json()["code"] == "JOB_FINISHED"
    events = client.get("/dps/v1/events", headers=SERVICE).json()["events"]
    assert [e["type"] for e in events] == ["dps.job.accepted", "dps.job.cancelled"]
    assert events[1]["payload"]["cancelledBy"] == "rm-anil"


def test_cancel_a_running_job_flags_it(client, app):
    job_id = submit(client, request_body(app)).json()["jobId"]
    with app.state.db.session_factory() as s:
        jobs.claim(s, "w1", "STANDARD", 60, 3)
    assert client.post(f"/dps/v1/jobs/{job_id}/cancel", headers=SERVICE).json()["cancelRequested"] is True
    with app.state.db.session_factory() as s:
        assert jobs.cancelled(s, job_id) and s.get(Job, job_id).status == jobs.RUNNING


# ---------- events ----------

def test_event_feed_pages_in_order_and_matches_the_contract(client, app):
    for n in range(3):
        submit(client, request_body(app, version=f"dv-{n}"), key=f"key-0000000{n}")
    page = client.get("/dps/v1/events?after=0&limit=2", headers=SERVICE).json()
    check_component("EventPage", page)
    assert [e["seq"] for e in page["events"]] == [1, 2] and page["nextCursor"] == 2
    rest = client.get(f"/dps/v1/events?after={page['nextCursor']}", headers=SERVICE).json()
    assert [e["seq"] for e in rest["events"]] == [3]
    empty = client.get(f"/dps/v1/events?after={rest['nextCursor']}", headers=SERVICE).json()
    assert empty == {"events": [], "nextCursor": 3}
    assert len({e["eventId"] for e in page["events"] + rest["events"]}) == 3


# ---------- batches ----------

def test_batch_runs_good_items_and_reports_bad_ones(client, app):
    good = [request_body(app, version=f"dv-{n}") for n in range(3)]
    bad = request_body(app, version="dv-bad")
    bad["document"]["sha256"] = "1" * 64
    duplicate = request_body(app, version="dv-0")
    items = [{"clientFileReference": f"RM-{n}", "job": body} for n, body in enumerate(good + [bad, duplicate])]
    r = client.post("/dps/v1/batches", json={"batchRef": "bulk-1", "items": items},
                    headers={**SERVICE, "Idempotency-Key": "batch-0001"})
    assert r.status_code == 202, r.text
    check_component("BatchAccepted", r.json())
    batch_id = r.json()["batchId"]
    view = client.get(f"/dps/v1/batches/{batch_id}", headers=SERVICE).json()
    check_component("Batch", view)
    assert view["counts"] == {"RUNNING": 3, "REJECTED": 1, "DUPLICATE": 1}
    csv_text = client.get(f"/dps/v1/batches/{batch_id}/errors.csv", headers=SERVICE).text
    assert csv_text.splitlines()[1].startswith("RM-3,") and "INTEGRITY_MISMATCH" in csv_text
    again = client.post("/dps/v1/batches", json={"batchRef": "bulk-1", "items": items},
                        headers={**SERVICE, "Idempotency-Key": "batch-0001"})
    assert again.status_code == 200 and again.json()["batchId"] == batch_id


def test_batch_completes_with_its_event_and_reruns_only_dead_items(client, app):
    items = [{"clientFileReference": f"RM-{n}", "job": request_body(app, version=f"dv-{n}")} for n in range(2)]
    batch_id = client.post("/dps/v1/batches", json={"batchRef": "b", "items": items},
                           headers={**SERVICE, "Idempotency-Key": "batch-0002"}).json()["batchId"]
    with app.state.db.session_factory() as s:
        first, second = s.query(Job).filter(Job.batch_id == batch_id).order_by(Job.client_file_reference).all()
        jobs.finish(s, first, jobs.DONE)
        jobs.finish(s, second, jobs.DEAD, "storage down", "STORAGE_UNAVAILABLE")
        assert s.get(Batch, batch_id).status == "COMPLETED"
        completed = [e for e in s.query(EventFeed) if e.type == "dps.batch.completed"]
        assert json.loads(completed[0].payload_json)["counts"] == {"SUCCEEDED": 1, "FAILED_RETRYABLE": 1}
    view = client.post(f"/dps/v1/batches/{batch_id}/rerun", headers=SERVICE).json()
    assert view["status"] == "RUNNING" and view["counts"] == {"SUCCEEDED": 1, "RUNNING": 1}


def test_batch_limits(client, app, settings):
    settings.batch_max_items = 1
    items = [{"clientFileReference": f"RM-{n}", "job": request_body(app, version=f"dv-{n}")} for n in range(2)]
    r = client.post("/dps/v1/batches", json={"batchRef": "b", "items": items},
                    headers={**SERVICE, "Idempotency-Key": "batch-0003"})
    assert r.status_code == 400 and r.json()["code"] == "BATCH_TOO_LARGE"


# ---------- catalogue and ops ----------

def test_catalogue_endpoints(client):
    names = client.get("/dps/v1/schemas", headers=SERVICE).json()
    assert [n["name"] for n in names] == list(SCHEMA_NAMES)
    assert client.get("/dps/v1/schemas/result", headers=SERVICE).json()["title"] == "dps.result/1.0"
    assert client.get("/dps/v1/schemas/nope", headers=SERVICE).status_code == 404
    caps = client.get("/dps/v1/capabilities", headers=SERVICE).json()
    check_component("Capabilities", caps)
    assert {t["jobType"] for t in caps["jobTypes"] if t["available"]} == {
        "EXTRACT_AGREEMENT", "EXTRACT_STATEMENT", "ANALYZE", "EXTRACT_SANCTION", "EXTRACT_CERTIFICATE",
        "EXTRACT_AMENDMENT", "DRAFT_COMMENTARY", "ONBOARD"}


def test_health_and_queue_report(client, app):
    assert client.get("/health/live").json() == {"status": "UP"}
    assert client.get("/health/ready").json()["database"] == "UP"
    submit(client, request_body(app))
    report = client.get("/dps/v1/ops/queues", headers=SUPPORT).json()
    check_component("QueueReport", report)
    assert report["rows"][0]["count"] == 1 and report["dead"] == 0
    assert client.get("/dps/v1/ops/queues", headers=SERVICE).status_code == 403


# ---------- queue rules ----------

def _queued(s, *, priority: int = 1, tenant: str = "default", batch: str | None = None, age_minutes: int = 0) -> str:
    doc = Document(type="LOAN_AGREEMENT", file_name="f.pdf", storage_path="x", sha256="h", size_bytes=1,
                   uploaded_by="u", status="QUEUED")
    s.add(doc)
    s.flush()
    created = utcnow() - timedelta(minutes=age_minutes)
    job = Job(document_id=doc.id, flow="AGREEMENT_INTAKE", priority=priority, tenant_id=tenant, batch_id=batch,
              created_at=created, due_at=created, updated_at=created)
    s.add(job)
    s.commit()
    return job.id


def test_interactive_job_jumps_the_bulk_queue(app):
    with app.state.db.session_factory() as s:
        for _ in range(30):
            _queued(s, priority=4, age_minutes=5)
        urgent = _queued(s, priority=1)
        assert jobs.claim(s, "w1", "STANDARD", 60, 3).id == urgent


def test_aging_moves_a_waiting_job_up_until_priority_one(app):
    with app.state.db.session_factory() as s:
        job_id = _queued(s, priority=4, age_minutes=20)
        for _ in range(3):
            assert jobs.age(s, 15) == 1
            s.expire_all()  # age() is one UPDATE statement; re-read the row
            job = s.get(Job, job_id)
            job.updated_at = utcnow() - timedelta(minutes=16)
            s.commit()
        s.expire_all()
        assert s.get(Job, job_id).priority == 1
        assert jobs.age(s, 15) == 0


def test_fairness_lets_a_small_tenant_through(app):
    limits = jobs.Limits(per_tenant=2)
    with app.state.db.session_factory() as s:
        for _ in range(5):
            _queued(s, tenant="big", age_minutes=10)
        small = _queued(s, tenant="small")
        claimed = [jobs.claim(s, f"w{n}", "STANDARD", 60, 3, limits=limits) for n in range(3)]
        assert [s.get(Job, j.id).tenant_id for j in claimed] == ["big", "big", "small"] and claimed[2].id == small
        assert jobs.claim(s, "w9", "STANDARD", 60, 3, limits=limits) is None  # big is at its limit


def test_batch_limit_holds_a_batch_to_its_share(app):
    limits = jobs.Limits(per_batch=1)
    with app.state.db.session_factory() as s:
        _queued(s, batch="B1", age_minutes=3)
        _queued(s, batch="B1", age_minutes=2)
        other = _queued(s, age_minutes=1)
        first = jobs.claim(s, "w1", "STANDARD", 60, 3, limits=limits)
        second = jobs.claim(s, "w2", "STANDARD", 60, 3, limits=limits)
        assert first.batch_id == "B1" and second.id == other


def test_retry_waits_longer_each_time_then_goes_dead(app):
    with app.state.db.session_factory() as s:
        job_id = _queued(s)
        job = jobs.claim(s, "w1", "STANDARD", 60, 3)
        jobs.release_for_retry(s, job, "down", 3, "SPRING_UNAVAILABLE", base_seconds=10, max_seconds=900)
        job = s.get(Job, job_id)
        wait = (jobs._aware(job.due_at) - utcnow()).total_seconds()  # noqa: SLF001
        assert job.status == jobs.RETRY_WAIT and 7 <= wait <= 13
        assert jobs.claim(s, "w1", "STANDARD", 60, 3) is None  # not due yet
        assert jobs.retry_delay(3, 10, 900) >= 128 and jobs.retry_delay(9, 10, 900) <= 1080
        job.attempts = 3
        jobs.release_for_retry(s, job, "still down", 3, "SPRING_UNAVAILABLE")
        assert s.get(Job, job_id).status == jobs.DEAD
        dead = [e for e in s.query(EventFeed) if e.type == "dps.job.dead"]
        assert json.loads(dead[0].payload_json)["errorCode"] == "SPRING_UNAVAILABLE"
        attempts = s.query(JobAttempt).filter(JobAttempt.job_id == job_id).all()
        assert [a.outcome for a in attempts] == [jobs.RETRY_WAIT]


def test_a_file_that_kills_its_worker_twice_is_set_aside(app):
    with app.state.db.session_factory() as s:
        job_id = _queued(s)
        other = _queued(s, age_minutes=0)
        for crash in range(2):
            job = s.get(Job, job_id)
            job.status, job.locked_until = jobs.RUNNING, utcnow() - timedelta(seconds=1)
            s.commit()
            if crash == 0:
                assert jobs.claim(s, "w1", "STANDARD", 60, 5).id == job_id
        assert jobs.claim(s, "w2", "STANDARD", 60, 5).id == other
        poisoned = s.get(Job, job_id)
        assert poisoned.status == jobs.DEAD and poisoned.error_code == "PARSER_CRASH"


def test_existing_job_columns_hash_and_feed(app):
    """The idempotency hash is stable for the same body regardless of key order."""
    from dps.api.jobs import JobRequest, _hash
    body = {"jobType": "EXTRACT_AGREEMENT", "context": {"requestedBy": "u"},
            "document": {"sha256": "a" * 64, "fileName": "f", "mediaType": "application/pdf", "sizeBytes": 1}}
    reordered = dict(reversed(list(body.items())))
    assert _hash(JobRequest(**body)) == _hash(JobRequest(**reordered))
    assert hashlib.sha256(b"").hexdigest() != _hash(JobRequest(**body))


# ---------- schemas added with the scaling decision (17 Nov 2026) ----------

def _def(name: str, part: str) -> dict:
    return {"$ref": f"{_schema(name)['$id']}#/$defs/{part}"}


def check_def(name: str, part: str, body) -> None:
    jsonschema.Draft202012Validator(_def(name, part), registry=_registry()).validate(body)


def test_review_decisions_require_value_for_correct_and_page_for_add():
    check("review-decisions", {"decisions": [{"fieldId": "f1", "action": "CONFIRM"}]})
    check("review-decisions", {"decisions": [{"fieldId": "f1", "action": "CORRECT", "value": 1.5,
                                              "reasonCode": "WRONG_VALUE"}]})
    with pytest.raises(jsonschema.ValidationError):
        check("review-decisions", {"decisions": [{"fieldId": "f1", "action": "CORRECT"}]})
    with pytest.raises(jsonschema.ValidationError):
        check("review-decisions", {"decisions": [{"fieldId": "n1", "action": "ADD", "value": 1,
                                                  "reasonCode": "MISSED", "group": "COVENANT", "name": "DSCR"}]})


def test_compat_schemas_keep_the_bank_app_enums():
    check_def("compat-data-values", "request", {"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]})
    check_def("compat-data-values", "response", {"cpId": "CP-1", "missingKeys": [],
                                                 "points": [{"asOfDate": "2026-03-31", "values": {"EBITDA": 10}}]})
    check_def("compat-covenant-status", "response", {"recommendations": [
        {"appliedCovenantId": "AC-1", "recommendedStatus": "COMPLIED", "rationale": "p.3", "confidence": 0.9}]})
    with pytest.raises(jsonschema.ValidationError):
        check_def("compat-covenant-status", "response", {"recommendations": [
            {"appliedCovenantId": "AC-1", "recommendedStatus": "COMPLIANT"}]})


def test_packaged_contract_has_every_schema_the_api_serves():
    for name in SCHEMA_NAMES:
        assert (PACKAGED / "schemas" / f"{name}.json").is_file(), name


def test_every_issue_code_a_result_can_carry_is_in_the_catalogue():
    """Spring looks every warning and field issue up in errors.yaml; a code missing there is a code nobody can explain
    (handover check, 19 Nov 2026: 41 were missing)."""
    import re

    codes = {e["code"] for e in yaml.safe_load((CONTRACTS / "catalogues" / "errors.yaml")
                                               .read_text(encoding="utf-8"))["errors"]}
    patterns = [re.compile(r'issue\(\s*"([A-Z][A-Z0-9_]+)"'), re.compile(r'_hold\(\w+, "([A-Z][A-Z0-9_]+)"'),
                re.compile(r'(?:issues|problems)\.append\(\s*f?"([A-Z][A-Z0-9_]{3,}):'),
                re.compile(r'(?:issues|problems)\s*\+?=\s*\[\s*f?"([A-Z][A-Z0-9_]{3,}):'),
                re.compile(r'issues=\[\s*f?"([A-Z][A-Z0-9_]{3,}):')]
    used = set()
    for path in (ROOT / "dps" / "src" / "dps").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for p in patterns:
            used |= set(p.findall(text))
    assert used and used <= codes, sorted(used - codes)
