"""Release checks for job context, lease ownership and document workflows."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from stub_spring import LIBRARY, StubSpring
from test_architect_gaps import AGREEMENT_TEXT, BASE
from test_flows import GOOD_ROWS
from test_flows import xlsx_bytes as statement_workbook
from test_jobapi import SERVICE, check, request_body
from test_jobapi import submit as submit_request
from test_phase2_ingest import text_pdf, xls_bytes, xlsx_bytes
from test_phase4_skills import AMENDMENT, CERTIFICATE, SANCTION, submit

from dps import jobs
from dps.integrations.spring import SpringClient
from dps.models import Document, Job, StatementValue, utcnow
from dps.skills import load, runner
from dps.worker import Worker, build_analyzer, build_ingestor, build_pack_runner


def test_same_document_with_different_business_contexts_creates_separate_jobs(client, app):
    body = request_body(app)
    first = submit_request(client, body).json()["jobId"]
    body["context"]["cpIds"] = ["CP-2"]
    body["context"]["cycleId"] = "cycle-new"
    second = submit_request(client, body, "second-context")
    assert second.status_code == 202, second.text
    second_id = second.json()["jobId"]
    assert second_id != first
    assert submit_request(client, body, "second-retry").json()["jobId"] == second_id
    with app.state.db.session_factory() as db:
        assert db.get(Document, db.get(Job, first).document_id).active_cp_ids() == ["CP-1"]
        assert db.get(Document, db.get(Job, second_id).document_id).active_cp_ids() == ["CP-2"]


def test_expired_worker_cannot_finish_reclaimed_job(client, app):
    job_id = submit_request(client, request_body(app)).json()["jobId"]
    with app.state.db.session_factory() as db:
        old = jobs.claim(db, "worker-a", "STANDARD", 120, 3)
        attempt = old.attempts
        old.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
        assert jobs.claim(db, "worker-b", "STANDARD", 120, 3).id == job_id
        token = jobs.worker_attempt.set((job_id, "worker-a", attempt))
        try:
            with pytest.raises(jobs.LeaseLost):
                jobs.finish(db, db.get(Job, job_id), jobs.DONE)
        finally:
            jobs.worker_attempt.reset(token)
        assert db.get(Job, job_id).status == jobs.RUNNING
        assert db.get(Job, job_id).locked_by == "worker-b"


def test_a_late_heartbeat_keeps_an_expired_lease_nobody_else_claimed(client, app):
    job_id = submit_request(client, request_body(app)).json()["jobId"]
    with app.state.db.session_factory() as db:
        job = jobs.claim(db, "worker-a", "STANDARD", 120, 3)
        attempt = job.attempts
        job.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
        assert jobs.renew_lease(db, job_id, "worker-a", 120, attempt)
        job = db.get(Job, job_id)
        job.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
        token = jobs.worker_attempt.set((job_id, "worker-a", attempt))
        try:
            jobs.finish(db, db.get(Job, job_id), jobs.DONE)
            db.commit()
        finally:
            jobs.worker_attempt.reset(token)
        assert db.get(Job, job_id).status == jobs.DONE


def test_amendment_ambiguous_metric_preserves_both_approved_records():
    base = runner.approved_base([*BASE, {**BASE[0], "appliedCovenantId": "AC-DSCR-OTHER"}])
    model = {"pages": [{"no": 1, "text": AMENDMENT}]}
    fields = load("amendment_diff").rules(model["pages"], {"base": base})
    assert any(issue["code"] == "AMBIGUOUS_AMENDMENT_TARGET" for f in fields for issue in f["issues"])
    from dps.skills.amendment_diff.rules import amended_set

    remaining = amended_set(base, fields)
    assert {"AC-11", "AC-DSCR-OTHER"} <= {f["value"].get("appliedCovenantId") for f in remaining}


def _worker(app, settings, tmp_path, monkeypatch):
    stub = StubSpring()

    def core_response(request):
        path = request.url.path
        if path == "/api/v1/covenants":
            return httpx.Response(200, json={"content": LIBRARY, "last": True})
        if path == "/api/v1/formulas/validate":
            return httpx.Response(200, json={"valid": True, "errors": []})
        if path == "/api/v1/data-events":
            return httpx.Response(200, json={})
        if path == "/api/v1/credit-proposals/CP-1/currency":
            return httpx.Response(200, json={"currency": "INR"})
        return stub.handle(request)

    http = httpx.Client(base_url="http://spring", transport=httpx.MockTransport(core_response))
    core = SpringClient("http://spring", 5, client=http)
    monkeypatch.setattr(SpringClient, "from_settings", classmethod(lambda cls, settings: core))
    settings.derived_dir = tmp_path / "derived"
    settings.staging_dir = tmp_path / "staging"
    db = app.state.db
    page_models = {}
    return Worker(settings, db.session_factory, core, {}, build_ingestor(settings), page_models,
                  build_analyzer(settings, db), build_pack_runner(settings, db, core, page_models))


def _docx() -> bytes:
    from docx import Document as WordDocument

    doc = WordDocument()
    for line in AGREEMENT_TEXT.splitlines():
        doc.add_paragraph(line)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _scan() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1650, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=32)
    lines = ["FACILITY AGREEMENT", "The Borrower shall maintain a Debt Service Coverage Ratio",
             "of not less than 1.20 times, tested quarterly.", "Borrower: Example Industries Limited"]
    for row, line in enumerate(lines):
        draw.text((55, 100 + 65 * row), line.replace(" ", "  "), fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150)
    return buffer.getvalue()


PDF = "application/pdf"
SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
STATEMENT = "Balance sheet as at 31 March 2026 (INR lakh)\nRevenue 1500\nEBITDA 400\nTotal assets 2000"
GOLDEN = Path(__file__).resolve().parents[2] / "golden-set" / "documents"


@pytest.mark.parametrize(("kind", "extension", "media", "fixture", "context"), [
    ("ANALYZE", ".pdf", PDF, lambda: text_pdf([AGREEMENT_TEXT]), {}),
    ("ONBOARD", ".pdf", PDF, lambda: text_pdf([AGREEMENT_TEXT]), {"expectedType": "LOAN_AGREEMENT"}),
    ("ONBOARD", ".docx", WORD, _docx, {"expectedType": "LOAN_AGREEMENT"}),
    ("ONBOARD", ".pdf", PDF, _scan, {"expectedType": "LOAN_AGREEMENT"}),
    ("EXTRACT_AGREEMENT", ".pdf", PDF, lambda: text_pdf([AGREEMENT_TEXT]), {}),
    ("EXTRACT_SANCTION", ".pdf", PDF, lambda: text_pdf([SANCTION]), {}),
    ("EXTRACT_AMENDMENT", ".pdf", PDF, lambda: text_pdf([AMENDMENT]), {"baseCovenants": BASE}),
    ("EXTRACT_CERTIFICATE", ".pdf", PDF, lambda: text_pdf([CERTIFICATE]), {}),
    ("EXTRACT_STATEMENT", ".pdf", PDF, lambda: text_pdf([STATEMENT]), {"asOfDate": "2026-03-31"}),
    ("EXTRACT_STATEMENT", ".pdf", PDF, lambda: (GOLDEN / "FS-023.pdf").read_bytes(), {"asOfDate": "2026-03-31"}),
    ("EXTRACT_STATEMENT", ".xlsx", SHEET, xlsx_bytes, {"asOfDate": "2026-03-31"}),
    ("EXTRACT_STATEMENT", ".xls", "application/vnd.ms-excel", xls_bytes, {"asOfDate": "2026-03-31"}),
    ("EXTRACT_STATEMENT", ".csv", "text/csv", lambda: b"Line,Amount\nRevenue,1200\nEBITDA,300\n",
     {"asOfDate": "2026-03-31"}),
    ("DRAFT_COMMENTARY", ".json", "application/json",
     lambda: json.dumps({"cpId": "CP-1", "covenants": [{"name": "DSCR", "result": "COMPLIANT"}]}).encode(), {}),
], ids=["analyze", "onboard-pdf", "onboard-word", "onboard-scan", "agreement", "sanction", "amendment", "certificate",
        "statement-pdf", "statement-scan", "statement-xlsx", "statement-xls", "statement-csv", "commentary"])
def test_document_job_matrix(client, app, settings, tmp_path, monkeypatch, kind, extension, media, fixture, context):
    worker = _worker(app, settings, tmp_path, monkeypatch)
    response = submit(client, app, kind, fixture(), extension, media, "matrix-version", "matrix-key", **context)
    assert response.status_code == 202, response.text
    job_id = response.json()["jobId"]
    assert worker.run_once()
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE)
    assert result.status_code == 200, result.text
    body = result.json()
    check("result", body)
    assert body["status"] in {"DONE", "NEEDS_REVIEW", "AUTO_ACCEPTED"}, body
    if kind == "DRAFT_COMMENTARY":
        assert body["commentary"]["paragraphs"], body
    elif fixture is _scan and not body["fields"]:
        assert body["status"] == "NEEDS_REVIEW"
        assert any(w["code"] == "TYPE_NOT_ONBOARDING" for w in body["warnings"])
    elif kind != "ANALYZE":
        assert body["fields"], body


def test_shadow_statement_preserves_preview_without_publishing(client, app, settings, tmp_path, monkeypatch):
    settings.rollout_default = "SHADOW"
    worker = _worker(app, settings, tmp_path, monkeypatch)
    workbook = statement_workbook(GOOD_ROWS)
    response = submit(client, app, "EXTRACT_STATEMENT", workbook, ".xlsx", SHEET, "shadow-version",
                      "shadow-key", asOfDate="2026-03-31")
    assert response.status_code == 202
    assert worker.run_once()
    job_id = response.json()["jobId"]
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert result["fields"]
    with app.state.db.session_factory() as db:
        assert db.get(Job, job_id).status == jobs.NEEDS_REVIEW  # every statement waits for a person (G21)
        assert db.query(StatementValue).count() == 0
