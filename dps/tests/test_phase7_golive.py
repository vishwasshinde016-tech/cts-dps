"""Phase 7: rollout modes (SHADOW, ASSISTED, NORMAL) and the go-live readiness report."""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import h
from sqlalchemy import select
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase4_skills import CERTIFICATE, make_worker, submit

from dps import jobs
from dps.models import EventFeed, Job, ReviewDecision, Run, utcnow

SERVICE = h("cts-spring", "SERVICE")
OPS = h("ops-1", "SUPPORT")


def test_shadow_mode_stores_but_emits_shadow_event(client, app, settings, tmp_path):
    settings.rollout_modes = {"EXTRACT_CERTIFICATE": "SHADOW"}
    r = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", "sh-1",
               "key-shadow-1")
    job_id = r.json()["jobId"]
    make_worker(app, settings, tmp_path).run_once()
    with app.state.db.session_factory() as s:
        assert s.get(Job, job_id).rollout == "SHADOW"
        events = {e.type: e for e in s.scalars(select(EventFeed).where(EventFeed.job_id == job_id))}
        types = set(events)
        assert "dps.extraction.shadow" in types and not types & {"dps.extraction.completed",
                                                                 "dps.extraction.needs_review"}
        from dps.feed import view

        shadow = view(events["dps.extraction.shadow"])
        check("event", shadow)
        assert shadow["payload"]["runId"] == job_id
        saved = s.scalar(select(Run).where(Run.job_id == job_id))
        assert shadow["payload"]["pipelineVersion"] == json.loads(saved.result_json)["pipeline"]["version"]
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert result["rollout"] == "SHADOW" and result["fields"]


def test_assisted_mode_sends_every_field_to_review(client, app, settings, tmp_path):
    settings.rollout_default = "ASSISTED"
    r = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", "as-1",
               "key-assist-1")
    job_id = r.json()["jobId"]
    make_worker(app, settings, tmp_path).run_once()
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert result["rollout"] == "ASSISTED" and all(f["status"] == "NEEDS_REVIEW" for f in result["fields"])


def test_go_live_report(client, app, settings, tmp_path):
    r = submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", "gl-1",
               "key-golive-1")
    job_id = r.json()["jobId"]
    make_worker(app, settings, tmp_path).run_once()
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        job.started_at = utcnow() - timedelta(seconds=40)
        job.finished_at = utcnow()
        s.add(ReviewDecision(job_id=job_id, run_no=2, field_id="f", reviewer="a", action="CONFIRM", sampled=True))
        s.commit()
    body = client.get("/dps/v1/ops/go-live", headers=OPS).json()
    cert = next(t for t in body["types"] if t["jobType"] == "EXTRACT_CERTIFICATE")
    assert cert["measured"]["groundingRate"] == 1.0 and cert["measured"]["sampledErrorRate"] == 0.0
    assert cert["checks"]["groundingRate"] == "PASS" and cert["checks"]["p95SecondsNative"] == "PASS"
    assert cert["checks"]["classificationAccuracy"] == "NO_DATA" and cert["readyToGoLive"] is False
    assert cert["rollout"] == "NORMAL"
    assert client.get("/dps/v1/ops/go-live", headers=SERVICE).status_code == 403
    from dps.ops import golive

    with app.state.db.session_factory() as s:
        ready = golive.report(s, 30, {"EXTRACT_CERTIFICATE": {"classificationAccuracy": 0.99,
                                                               "calibrationError": 0.03}})
    cert = next(t for t in ready["types"] if t["jobType"] == "EXTRACT_CERTIFICATE")
    assert cert["readyToGoLive"] is True and json.dumps(ready)
    assert jobs.DONE
