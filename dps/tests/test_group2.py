"""Group 2 deployment and operations regressions."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import httpx
from conftest import h
from sqlalchemy import select
from test_jobapi import check
from test_phase4_skills import fake_job

from dps import feed, jobs
from dps.integrations.spring import SpringClient
from dps.models import AiCall, AuditEvent, Job, TraceEvent, utcnow
from dps.ops.production import prune_diagnostics, scaling
from dps.settings import Settings
from dps.skills import load, runner
from dps.skills.runner import token_budget
from dps.worker import heartbeat_healthy


def test_amendments_have_the_agreed_default_token_budget():
    assert token_budget(load("amendment_diff"), {"pages": [{}] * 250}) == 300_000


def test_long_amendment_reaches_pages_past_the_previous_limit(monkeypatch):
    page_text = "Administrative terms and conditions. " + "background " * 340
    pages = [{"no": no, "text": page_text} for no in range(1, 26)]
    pages[0]["text"] += "Amendment to the agreement."
    read = []

    def proposals(pack, gateway, batches, **kwargs):
        if pack.name == "amendment_diff":
            read.extend([[page["no"] for page in batch] for batch in batches])
        return []

    monkeypatch.setattr(runner, "batched_proposals", proposals)
    runner.run_job(fake_job("EXTRACT_AMENDMENT"), {"pages": pages})
    assert len(read) > 1
    assert [page for batch in read for page in batch] == list(range(1, 26))


def test_production_starts_in_shadow_unless_another_mode_is_set():
    assert Settings(env="prod").rollout_default == "SHADOW"
    assert Settings(env="prod", rollout_default="NORMAL").rollout_default == "NORMAL"


def test_heartbeat_requires_a_recent_write(settings, tmp_path):
    settings.worker_heartbeat_file = tmp_path / "heartbeat"
    assert not heartbeat_healthy(settings)
    settings.worker_heartbeat_file.touch()
    assert heartbeat_healthy(settings)
    stale = settings.worker_heartbeat_file.stat().st_mtime - settings.worker_lease_seconds - 10
    os.utime(settings.worker_heartbeat_file, (stale, stale))
    assert not heartbeat_healthy(settings)


def test_spring_probe_does_not_determine_dps_readiness(client, settings, monkeypatch):
    settings.core_health_path = "/actuator/health"
    monkeypatch.setattr(SpringClient, "health", lambda self, path: "DOWN")
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["spring"] == "DOWN"


def test_spring_probe_checks_actuator_status():
    def respond(request):
        assert request.url.path == "/actuator/health"
        return httpx.Response(200, json={"status": "UP"})

    with httpx.Client(base_url="http://spring", transport=httpx.MockTransport(respond)) as client:
        spring = SpringClient("http://spring", 2, client=client)
        assert spring.health("/actuator/health") == "UP"
        assert spring.health("https://example.com/actuator/health") == "DOWN"


def test_go_live_reads_only_evidence_for_the_running_pipeline(client, settings, tmp_path):
    path = tmp_path / "eval.json"
    settings.go_live_evaluation_file = str(path)
    evaluation = {"pipelineVersion": settings.pipeline_version, "jobTypes": {
        "EXTRACT_AGREEMENT": {"classificationAccuracy": 0.99, "calibrationError": 0.04}}}
    path.write_text(json.dumps(evaluation), encoding="utf-8")
    headers = h("ops", "SUPPORT")

    def agreement():
        types = client.get("/dps/v1/ops/go-live", headers=headers).json()["types"]
        return next(row for row in types if row["jobType"] == "EXTRACT_AGREEMENT")

    assert agreement()["checks"]["classificationAccuracy"] == "PASS"
    assert agreement()["checks"]["calibrationError"] == "PASS"
    path.write_text(json.dumps({**evaluation, "pipelineVersion": "another-build"}), encoding="utf-8")
    assert agreement()["checks"]["classificationAccuracy"] == "NO_DATA"


def test_diagnostic_cleanup_is_opt_in_and_keeps_audit_and_fresh_rows(app):
    now = utcnow()
    with app.state.db.session_factory() as db:
        for age, label in ((100, "old"), (3, "fresh")):
            at = now - timedelta(days=age)
            db.add(AiCall(at=at, purpose="EXTRACT_AGREEMENT", user="worker", model="test", prompt_version=label,
                          status="OK"))
            db.add(TraceEvent(at=at, document_id="doc", step="READ", actor="worker", summary=label))
        db.add(AuditEvent(at=now - timedelta(days=100), actor="worker", action="TEST",
                          target_type="job", target_id="job"))
        db.commit()
        prune_diagnostics(db, 0)
        assert set(db.scalars(select(AiCall.prompt_version))) == {"old", "fresh"}
        prune_diagnostics(db, 90)
        assert list(db.scalars(select(AiCall.prompt_version))) == ["fresh"]
        assert list(db.scalars(select(TraceEvent.summary))) == ["fresh"]
        assert list(db.scalars(select(AuditEvent.action))) == ["TEST"]


def test_scaling_uses_only_eligible_queue_items(app):
    from dps.models import Document

    now = utcnow()
    with app.state.db.session_factory() as db:
        doc = Document(file_name="test.pdf", storage_path="x", sha256="a" * 64,
                       size_bytes=1, uploaded_by="test", type="LOAN_AGREEMENT")
        db.add(doc)
        db.flush()
        db.add_all([Job(document_id=doc.id, job_type="AUTO", pool=pool, due_at=now + timedelta(seconds=offset),
                        status=status, flow="SKILL_PACK")
                    for pool, offset, status in (("STANDARD", 120, jobs.QUEUED),
                                                  ("STANDARD", -120, jobs.RETRY_WAIT),
                                                  ("HEAVY", -2, jobs.QUEUED))])
        db.commit()
        pools = {item["pool"]: item for item in scaling(db)}
    assert pools["STANDARD"]["queued"] == 1
    assert pools["STANDARD"]["advice"] == "SCALE_OUT"
    assert pools["HEAVY"]["queued"] == 1 and pools["HEAVY"]["oldestQueuedSeconds"] < 60


def test_queue_metrics_use_gauges_for_current_values(app, monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from dps.ops import observability as obs
    from dps.worker import Worker

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setitem(obs._otel, "meter", provider.get_meter("group2-test"))
    monkeypatch.setitem(obs._otel, "instruments", {})
    try:
        with app.state.db.session_factory() as db:
            Worker._queue_metrics(db)
        metrics = {metric.name: metric.data for resource in reader.get_metrics_data().resource_metrics
                   for scope in resource.scope_metrics for metric in scope.metrics}
        points = metrics["dps.queue.ready"].data_points
        assert {point.attributes["pool"]: point.value for point in points} == {"STANDARD": 0, "HEAVY": 0}
    finally:
        provider.shutdown()


def test_feed_preserves_historical_payloads_and_versions_new_ones(app):
    from dps.models import EventFeed

    with app.state.db.session_factory() as db:
        db.add(EventFeed(type="dps.job.moved", tenant_id="default", payload_json='{"pool":"HEAVY"}'))
        feed.emit_for_job(db, "dps.extraction.completed", Job(id="job-1", pipeline_version="release-1"),
                          {"resultUrl": "/dps/v1/jobs/job-1/result", "runId": "job-1:2"})
        db.commit()
        past, current = [feed.view(row) for row in db.scalars(select(EventFeed).order_by(EventFeed.seq))]
    check("event", past)
    check("event", current)
    assert "schemaVersion" not in past["payload"]
    assert current["payload"] == {"resultUrl": "/dps/v1/jobs/job-1/result", "runId": "job-1:2",
                                  "pipelineVersion": "release-1", "schemaVersion": "1.0"}
