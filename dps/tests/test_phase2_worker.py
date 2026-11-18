"""Phase 2 in the worker: hash check, inspection and the page model run before the flow; the flow reads it."""

from __future__ import annotations

import gzip
import json

from conftest import h
from sqlalchemy import select
from test_phase2_ingest import AGREEMENT, js_pdf, text_pdf

from dps import jobs
from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.models import Artifact, EventFeed, Job, utcnow
from dps.parse import sandbox
from dps.parse.page_model import to_pages
from dps.parsing import ParseError
from dps.worker import PageModelFirst, Worker

SERVICE = h("cts-spring", "SERVICE")


def submit(client, app, data: bytes, version: str, key: str) -> str:
    digest, obj = app.state.store.put(data, ".pdf")
    body = {"jobType": "EXTRACT_AGREEMENT", "priority": "INTERACTIVE",
            "context": {"tenantId": "default", "cpIds": ["CP-1"], "requestedBy": "rm-anil"},
            "document": {"versionId": version, "objectKey": obj, "sha256": digest, "fileName": "f.pdf",
                         "mediaType": "application/pdf", "sizeBytes": len(data)}}
    r = client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": key})
    assert r.status_code in (200, 201, 202), r.text
    return r.json()["jobId"]


def make_worker(app, settings, tmp_path, calls: list, parse=None):
    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    counted = []

    def in_process(req, hint):
        counted.append(req.sha256)
        return (parse or sandbox.parse_in_process)(req)

    page_models: dict = {}

    def runner(job: Job):
        calls.append(job.id)
        return "PUBLISHED", "state-1", ""

    ingestor = Ingestor(store, settings.pipeline_version,
                        lambda req, hint: in_process(req.__class__(req.data, req.fmt, req.sha256, req.version_id,
                                                                    False, False), hint), ocr=False)
    w = Worker(settings, app.state.db.session_factory, object(), {"AGREEMENT_INTAKE": runner}, ingestor, page_models)
    return w, counted, page_models, store


def test_page_model_built_once_saved_and_reused(client, app, settings, tmp_path):
    data = text_pdf([AGREEMENT])
    calls: list = []
    w, counted, page_models, store = make_worker(app, settings, tmp_path, calls)
    first = submit(client, app, data, "dv-1", "key-00000001")
    second = submit(client, app, data, "dv-2", "key-00000002")
    assert w.run_once() and w.run_once()
    assert len(counted) == 1  # same bytes, same pipeline version: parsed once
    with app.state.db.session_factory() as s:
        assert {s.get(Job, i).status for i in (first, second)} == {jobs.DONE}
        arts = s.scalars(select(Artifact)).all()
        assert len(arts) == 1 and arts[0].path == "default/dv-1/page-model/1/page-model.json.gz"
        model = json.loads(gzip.decompress(store.get_derived(arts[0].path, arts[0].sha256)))
    assert model["documentVersionId"] == "dv-1" and model["pageCount"] == 1
    assert page_models == {}  # cleared after each job


def test_flow_parser_reads_the_page_model(tmp_path):
    data = text_pdf([AGREEMENT])
    model = sandbox.parse_in_process(sandbox.ParseRequest(data, "PDF", "x", ocr=False))
    import hashlib

    class Boom:
        def parse(self, _):
            raise ParseError("should not parse again")

    parser = PageModelFirst(Boom(), {hashlib.sha256(data).hexdigest(): model})
    assert parser.parse(data) == to_pages(model)


def test_hostile_file_fails_with_code_and_quarantine_event(client, app, settings, tmp_path):
    calls: list = []
    w, _, _, _ = make_worker(app, settings, tmp_path, calls)
    job_id = submit(client, app, js_pdf(), "dv-9", "key-00000009")
    w.run_once()
    assert calls == []  # never reached the flow
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.error_code) == (jobs.FAILED, "UNSAFE_CONTENT")
        event = s.scalars(select(EventFeed).where(EventFeed.job_id == job_id,
                                                  EventFeed.type == "dps.job.failed")).one()
        assert json.loads(event.payload_json)["quarantine"] is True


def test_integrity_mismatch_and_parser_crash(client, app, settings, tmp_path):
    calls: list = []
    w, _, _, _ = make_worker(app, settings, tmp_path, calls)
    job_id = submit(client, app, text_pdf([AGREEMENT]), "dv-3", "key-00000003")
    with app.state.db.session_factory() as s:
        path = app.state.store.path(_doc_path(s, job_id))
    path.write_bytes(b"%PDF-1.4 tampered")
    w.run_once()
    with app.state.db.session_factory() as s:
        assert s.get(Job, job_id).error_code == "INTEGRITY_MISMATCH"

    def crash(req):
        raise sandbox.ParserCrash("boom")

    w2, _, _, _ = make_worker(app, settings, tmp_path, calls, parse=crash)
    crash_id = submit(client, app, text_pdf(["SANCTION LETTER\n" + AGREEMENT]), "dv-4", "key-00000004")
    w2.run_once()
    with app.state.db.session_factory() as s:
        job = s.get(Job, crash_id)
        assert (job.status, job.error_code) == (jobs.RETRY_WAIT, "PARSER_CRASH")
        job.due_at = utcnow()
        s.commit()
    w2.run_once()
    with app.state.db.session_factory() as s:
        assert s.get(Job, crash_id).status == jobs.DEAD  # second crash on the same file: poison


def _doc_path(s, job_id):
    from dps.models import Document

    return s.get(Document, s.get(Job, job_id).document_id).storage_path


def test_parse_timeout_moves_the_job_to_the_heavy_pool(client, app, settings, tmp_path):
    """Found by the end-to-end run (17 Nov): a scan that is too slow for the LIGHT pool goes to HEAVY, not FAILED."""
    calls: list = []

    def slow(req):
        raise sandbox.ParseTimeout("Parsing took longer than 120 s.")

    w, _, _, _ = make_worker(app, settings, tmp_path, calls, parse=slow)
    job_id = submit(client, app, text_pdf([AGREEMENT]), "dv-slow", "key-slow-0001")
    w.run_once()
    with app.state.db.session_factory() as s:
        job = s.get(Job, job_id)
        assert (job.status, job.pool, job.attempts) == (jobs.QUEUED, "HEAVY", 0)
    assert calls == []
