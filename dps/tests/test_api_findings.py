"""The API check of 21 Nov 2026: every reply matches its contract exactly, the listJobs filters work, the usage
report reads on SQLite, and the event catalogue lists the payload the code sends."""

from __future__ import annotations

import json
from datetime import timedelta

import yaml
from conftest import h
from test_jobapi import CONTRACTS, check, check_component, openapi, request_body, submit

from dps import feed, jobs
from dps.models import AiUsage, Job, utcnow

SERVICE = h("cts-spring", "SERVICE")
SUPPORT = h("ops-1", "SUPPORT")


def test_ops_usage_reads_its_rows_on_sqlite(client, app):
    """Was a 500: CAST(at AS DATE) gives a number on SQLite, so the date column could not be read."""
    with app.state.db.session_factory() as db:
        job = db.get(Job, submit(client, request_body(app)).json()["jobId"])
        for tokens in (100, 50):
            db.add(AiUsage(purpose="EXTRACT_AGREEMENT", user="rm-anil", job_id=job.id, model="azure/gpt",
                           calls=1, prompt_tokens=tokens, completion_tokens=10, at=utcnow() - timedelta(minutes=5)))
        db.commit()
    r = client.get("/dps/v1/ops/usage", headers=SUPPORT)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["calls"] == 2 and rows[0]["tokensIn"] == 150 and rows[0]["tokensOut"] == 20
    assert rows[0]["jobType"] == "EXTRACT_AGREEMENT" and rows[0]["day"] == utcnow().date().isoformat()
    check_component("UsageReport", r.json())


def test_list_jobs_filters_by_job_type_and_batch(client, app):
    agreement = submit(client, request_body(app, version="dv-a"), key="key-list-0001").json()["jobId"]
    statement = submit(client, request_body(app, job_type="EXTRACT_STATEMENT", version="dv-s"),
                       key="key-list-0002").json()["jobId"]
    batch = client.post("/dps/v1/batches", headers={**SERVICE, "Idempotency-Key": "key-list-batch1"},
                        json={"batchRef": "b-1", "items": [{"clientFileReference": "r-1",
                                                            "job": request_body(app, version="dv-b")}]}).json()
    only = lambda q: {j["id"] for j in client.get(f"/dps/v1/jobs?{q}", headers=SUPPORT).json()}  # noqa: E731
    assert agreement in only("jobType=EXTRACT_AGREEMENT") and statement not in only("jobType=EXTRACT_AGREEMENT")
    assert only("jobType=EXTRACT_STATEMENT") == {statement}
    in_batch = only(f"batchId={batch['batchId']}")
    assert len(in_batch) == 1 and agreement not in in_batch and statement not in in_batch
    for job in client.get("/dps/v1/jobs", headers=SUPPORT).json():
        check("job-status", job)


def test_job_replies_match_the_schema_exactly(client, app):
    """job-status.json is strict now: a field the code adds must be in the contract first."""
    job_id = submit(client, request_body(app)).json()["jobId"]
    assert json.loads((CONTRACTS / "schemas" / "job-status.json").read_text())["additionalProperties"] is False
    check("job-status", client.get(f"/dps/v1/jobs/{job_id}", headers=SERVICE).json())
    check("job-status", client.post(f"/dps/v1/jobs/{job_id}/cancel", headers=SERVICE).json())
    check_component("QueueReport", client.get("/dps/v1/ops/queues", headers=SUPPORT).json())
    for name in ("QueueReport",):
        assert openapi()["components"]["schemas"][name].get("additionalProperties") is False


def test_review_schemas_list_counts_and_the_reply_to_a_save():
    review = json.loads((CONTRACTS / "schemas" / "review.json").read_text())
    assert "counts" in review["properties"]
    status = json.loads((CONTRACTS / "schemas" / "job-status.json").read_text())
    assert {"revision", "remaining", "flow", "steps", "currentStep", "updatedAt"} <= set(status["properties"])


def test_contract_lists_get_health():
    assert "get" in openapi()["paths"]["/health"]


def test_event_catalogue_lists_the_payload_the_code_sends(client, app):
    events = {e["type"]: set((e.get("payload") or {}).keys())
              for e in yaml.safe_load((CONTRACTS / "catalogues" / "events.yaml").read_text())["events"]}
    job_id = submit(client, request_body(app)).json()["jobId"]
    with app.state.db.session_factory() as db:
        job = db.get(Job, job_id)
        jobs.finish(db, job, jobs.NEEDS_REVIEW, payload={"skillPack": "covenants/3.1.0", "fields": 3})
    sent, _ = feed.read(app.state.db.session_factory(), 0, 50)
    by_type = {e["type"]: set(e["payload"]) for e in sent}
    for kind in ("dps.job.accepted", "dps.extraction.needs_review"):
        assert by_type[kind] <= events[kind], (kind, by_type[kind] - events[kind])
    assert {"skillPack", "fields", "resultUrl"} <= events["dps.extraction.completed"]
    assert {"revision", "reviewedBy"} <= events["dps.extraction.completed"]
    assert {"cancelledBy", "errorCode", "message"} <= events["dps.job.cancelled"]
