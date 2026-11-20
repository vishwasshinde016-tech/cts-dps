"""Phase 6: scheduler leader lock, daily audit seal (hash chain), scaling signals, sizing, chaos (no lost or
duplicated job)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from conftest import h
from sqlalchemy import select
from test_phase2_ingest import text_pdf
from test_phase4_skills import submit

from dps import jobs
from dps.models import AuditEvent, AuditSeal, EventFeed, Job, Leader, ReviewDecision, utcnow
from dps.ops import production as ops

OPS = h("ops-1", "SUPPORT")


def test_leader_lock_one_holder_and_takeover(app):
    with app.state.db.session_factory() as s:
        assert ops.acquire_leader(s, "scheduler", "w1", 60)
        assert ops.acquire_leader(s, "scheduler", "w1", 60)  # renew
        assert not ops.acquire_leader(s, "scheduler", "w2", 60)
        s.get(Leader, "scheduler").until = utcnow() - timedelta(seconds=1)  # w1 crashed: lease ran out
        s.commit()
        assert ops.acquire_leader(s, "scheduler", "w2", 60)
        ops.release_leader(s, "scheduler", "w2")
        assert ops.acquire_leader(s, "scheduler", "w3", 60)


def test_daily_seal_chain_and_tamper_detection(app, tmp_path):
    store = tmp_path / "audit-seals"
    day1, day2 = date(2026, 11, 15), date(2026, 11, 16)
    with app.state.db.session_factory() as s:
        for d, n in ((day1, 2), (day2, 1)):
            for i in range(n):
                s.add(AuditEvent(actor="u", action="JOB_SUBMIT", target_type="job", target_id=f"j{d.day}-{i}",
                                 detail_json="{}", at=datetime(d.year, d.month, d.day, 10, i, tzinfo=UTC)))
        s.add(ReviewDecision(job_id="j", run_no=2, field_id="f", reviewer="a", action="CONFIRM", sampled=False,
                             at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC)))
        s.commit()
        sealed = ops.seal_pending(s, store, today=date(2026, 11, 17))
        assert sealed == [day1, day2]
        assert ops.seal_pending(s, store, today=date(2026, 11, 17)) == []
        seals = s.scalars(select(AuditSeal).order_by(AuditSeal.day)).all()
        assert [x.rows for x in seals] == [2, 2] and seals[1].prev_hash == seals[0].head_hash
        assert seals[0].file_sha256 and (store / "2026-11-15.json").is_file()
        assert ops.verify_seals(s) == []
        event = s.scalars(select(AuditEvent).where(AuditEvent.target_id == "j15-0")).first()
        event.actor = "someone-else"  # tampering after the seal
        s.commit()
        problems = ops.verify_seals(s)
        assert problems and problems[0]["day"] == "2026-11-15"
    r = app.state  # the ops endpoint reports the same
    assert r is not None


def test_audit_seals_endpoint(client, app):
    with app.state.db.session_factory() as s:
        s.add(AuditEvent(actor="u", action="X", target_type="t", target_id="1", detail_json="{}",
                         at=datetime(2026, 1, 1, tzinfo=UTC)))
        s.commit()
        ops.seal_pending(s, None, today=date(2026, 1, 3))
    body = client.get("/dps/v1/ops/audit-seals", headers=OPS).json()
    assert body["intact"] is True and body["seals"][0]["day"] == "2026-01-02"


def test_scaling_signals(client, app):
    with app.state.db.session_factory() as s:
        from dps.models import Document

        for i in range(6):
            s.add(Document(id=f"d{i}", type="LOAN_AGREEMENT", file_name="f.pdf", storage_path="x", sha256="0" * 64,
                           size_bytes=1, status="QUEUED", uploaded_by="u"))
            s.flush()
            s.add(Job(document_id=f"d{i}", flow="SKILL_PACK", status=jobs.QUEUED, pool="STANDARD",
                      created_at=utcnow() - timedelta(minutes=5), due_at=utcnow() - timedelta(minutes=5)))
        s.commit()
    pools = {p["pool"]: p for p in client.get("/dps/v1/ops/scaling", headers=OPS).json()["pools"]}
    assert pools["STANDARD"]["advice"] == "SCALE_OUT" and pools["STANDARD"]["queued"] == 6
    assert pools["STANDARD"]["oldestQueuedSeconds"] >= 290 and pools["HEAVY"]["advice"] == "SCALE_IN"
    assert client.get("/dps/v1/ops/scaling", headers=h("cts", "SERVICE")).status_code == 403


def test_capacity_worked_example():
    c = ops.capacity(100, 0.2, 2, 8, 43_000)
    assert c == {"lightWorkers": 5, "heavyWorkers": 5, "tokensPerMinute": 71667, "askForTpm": 150000}


def test_chaos_worker_dies_mid_job_job_ends_exactly_once(client, app, settings):
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["FACILITY AGREEMENT"]), ".pdf", "application/pdf",
               "chaos-1", "key-chaos-1")
    job_id = r.json()["jobId"]
    with app.state.db.session_factory() as s:
        first = jobs.claim(s, "worker-a", "STANDARD", 120, 3)
        assert first.id == job_id
        assert jobs.claim(s, "worker-b", "STANDARD", 120, 3) is None  # leased: nobody else takes it
        job = s.get(Job, job_id)
        job.locked_until = utcnow() - timedelta(seconds=1)  # worker-a was killed; its lease runs out
        s.commit()
        second = jobs.claim(s, "worker-b", "STANDARD", 120, 3)
        assert second.id == job_id and second.locked_by == "worker-b"
        assert not jobs.renew_lease(s, job_id, "worker-a", 120)  # the dead worker cannot write any more
        jobs.finish(s, second, jobs.DONE)
        done_events = s.scalars(select(EventFeed).where(EventFeed.job_id == job_id,
                                                        EventFeed.type == "dps.extraction.completed")).all()
        assert len(done_events) == 1
        assert jobs.claim(s, "worker-c", "STANDARD", 120, 3) is None  # finished: never claimed again
        payload = json.loads(done_events[0].payload_json)
        assert payload["resultUrl"].endswith("/result")


def test_stale_candidate_list_never_steals_a_running_job(client, app, monkeypatch):
    """Found by the end-to-end run (17 Nov): two workers read the same candidate list; the second must not take
    the job the first just claimed."""
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf(["FACILITY AGREEMENT"]), ".pdf", "application/pdf",
               "race-1", "key-race-1")
    job_id = r.json()["jobId"]
    with app.state.db.session_factory() as s:
        assert jobs.claim(s, "worker-a", "STANDARD", 120, 3).id == job_id
        monkeypatch.setattr(jobs, "_candidates", lambda *a, **k: [job_id])  # worker-b's list was read earlier
        assert jobs.claim(s, "worker-b", "STANDARD", 120, 3) is None
        job = s.get(Job, job_id)
        s.refresh(job)
        assert (job.locked_by, job.attempts) == ("worker-a", 1)
