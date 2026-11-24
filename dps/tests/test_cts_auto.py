"""CTS integration P3 and P4 (23 Nov 2026): one upload for every document type (AUTO), photos, the statement's
period from the document, certificates for the bank app's status recommender, figures by obligor, amendments
against the app's formulas, waiver extensions, reporting deadlines linked to covenants, sanction coverage, page
images for PDFs only, the AI budget of long documents and plain error messages."""

from __future__ import annotations

import io
import json
from datetime import date

import pytest
from conftest import h
from test_flows import FakeParser, StatementDeps, add_document, database, values, xlsx_bytes  # noqa: F401
from test_phase2_ingest import text_pdf
from test_phase4_skills import Tools, make_worker, submit

from dps import jobs
from dps.models import Document, DocumentCpLink, Job, Run
from dps.skills import runner as skills

SERVICE = h("cts-spring", "SERVICE")
AGREEMENT = ("FACILITY AGREEMENT\n21. FINANCIAL COVENANTS\n21.1 The Borrower shall maintain a Debt Service Coverage "
             "Ratio of not less than 1.25 times.\n21.2 The Borrower shall maintain an Interest Coverage Ratio of not "
             "less than 2.00 times.\n22. The Borrower shall deliver its quarterly unaudited financial statements "
             "within 45 days of the end of each quarter.")


def _png(size=(40, 20)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


# ---------- 35 the statement's period from its heading

@pytest.mark.parametrize("text,expected", [
    ("Balance Sheet as at 31 March 2026", date(2026, 3, 31)),
    ("Statement of Profit and Loss for the year ended 31st March, 2026", date(2026, 3, 31)),
    ("Unaudited results for the quarter ended 30.06.2025", date(2025, 6, 30)),
    ("For the period ending March 31, 2026", date(2026, 3, 31)),
    ("Half year ended 2025-09-30", date(2025, 9, 30)),
    ("as at 31 March 2026 and 31 March 2025", date(2026, 3, 31)),  # this year, not the comparative
    ("for the year ended 03/31/2026", date(2026, 3, 31)),  # only valid month-first
])
def test_the_period_end_is_read_from_the_heading(text, expected):
    from dps.skills.statement_lines.period import period_end

    assert period_end(text) == expected


def test_a_date_the_heading_does_not_tie_to_the_period_is_never_taken():
    from dps.skills.statement_lines.period import period_end

    assert period_end("Signed at Mumbai on 14 May 2026 by the Board of Directors") is None
    assert period_end("as at 31 February 2026") is None  # not a date


def _statement_job(database, rows, as_of=None, obligor=None):  # noqa: F811 - the fixture's database
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fs.xlsx", as_of)
    with database.session_factory() as s:
        ctx = {"requestedBy": "rm", **({"obligorId": obligor} if obligor else {})}
        job = Job(id=f"js-{doc_id[:8]}", document_id=doc_id, flow="SKILL_PACK", job_type="EXTRACT_STATEMENT",
                  request_json=json.dumps({"context": ctx}))
        s.add(job)
        s.commit()
        job_id = job.id
    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(rows), FakeParser([]), True)
    return doc_id, job_id, deps


ROWS = [("Balance Sheet as at 31 March 2026", None), ("Amounts in INR lakhs", None), ("EBITDA", 500),
        ("Short-term borrowings", 300), ("Long-term borrowings", 700), ("Total equity", 2000),
        ("Cash and cash equivalents", 100)]


def test_a_statement_without_a_date_takes_the_period_from_its_heading(database):  # noqa: F811
    doc_id, job_id, deps = _statement_job(database, ROWS)
    with database.session_factory() as s:
        job = s.get(Job, job_id)
        fields, warnings, _ = skills._statement(job, s, deps)  # noqa: SLF001
        s.commit()
        assert s.get(Document, doc_id).as_of_date == date(2026, 3, 31)
    assert "PERIOD_FROM_DOCUMENT" in {w["code"] for w in warnings}
    assert {f["value"]["asOfDate"] for f in fields} == {"2026-03-31"}


def test_a_statement_with_no_date_anywhere_is_held(database):  # noqa: F811
    _, job_id, deps = _statement_job(database, ROWS[1:])
    with database.session_factory() as s:
        _, warnings, _ = skills._statement(s.get(Job, job_id), s, deps)  # noqa: SLF001
    assert "PERIOD_DATE_MISSING" in {w["code"] for w in warnings}
    assert "PERIOD_DATE_MISSING" in skills.BLOCKING


def test_a_sent_date_is_never_replaced(database):  # noqa: F811
    _, job_id, deps = _statement_job(database, ROWS, as_of=date(2025, 12, 31))
    with database.session_factory() as s:
        fields, warnings, _ = skills._statement(s.get(Job, job_id), s, deps)  # noqa: SLF001
    assert not {"PERIOD_FROM_DOCUMENT", "PERIOD_DATE_MISSING"} & {w["code"] for w in warnings}
    assert {f["value"]["asOfDate"] for f in fields} == {"2025-12-31"}


# ---------- 39 figures by obligor id

def test_approved_figures_are_found_by_obligor_id_too(database):  # noqa: F811
    from dps.skills.statement_lines.reader import StatementReader, as_result, publish

    doc_id, job_id, deps = _statement_job(database, ROWS, as_of=date(2026, 3, 31), obligor="OB-9")
    state = StatementReader(deps).read(job_id, doc_id)
    with database.session_factory() as s:
        fields, warnings, extra = as_result(state, date(2026, 3, 31))
        for f in fields:
            f["status"] = "CONFIRMED"
        publish(s, s.get(Job, job_id), {"fields": fields, "warnings": warnings, **extra})
        s.commit()
    assert values(database, "OB-9")["EBITDA"].value == values(database, "CP-1")["EBITDA"].value == 5.0


# ---------- 34 AUTO and 44 photos

def test_auto_classifies_and_reads_an_agreement(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "AUTO", text_pdf([AGREEMENT]), ".pdf", "application/pdf", "auto-1", "key-auto-0001")
    assert r.status_code == 202, r.text
    assert w.run_once()
    result = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    assert result["classification"]["type"] == "LOAN_AGREEMENT"
    metrics = {f["value"].get("metric") for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT"}
    assert {"DSCR", "ICR"} <= metrics


def test_auto_reads_a_photo_and_says_what_it_cannot_read(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "AUTO", _png(), ".png", "image/png", "auto-photo-1", "key-auto-photo")
    assert r.status_code == 202, r.text
    assert w.run_once()
    job = client.get(f"/dps/v1/jobs/{r.json()['jobId']}", headers=SERVICE).json()
    assert job["status"] == "NEEDS_REVIEW", job  # a blank photo: nothing read, a person looks
    review = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/review", headers=SERVICE).json()
    assert all(f["evidence"]["pageImage"] is None for f in review["fields"])  # 45: no page images for a photo


def _statement_worker(app, settings, tmp_path):
    """make_worker with the statement reader's dependencies, as the real worker has."""
    from dps.ingest.pipeline import Ingestor
    from dps.ingest.store import LocalStore
    from dps.parse import sandbox
    from dps.worker import Worker

    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)
    deps = StatementDeps(app.state.db.session_factory, lambda doc: app.state.store.path(doc.object_key).read_bytes(),
                         FakeParser([]), True)

    def pack_runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, None, Tools(), db, "1", raw, statement_deps=deps))

    return Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, None, pack_runner)


def test_auto_reads_a_statement_and_publishes_it_after_review(client, app, settings, tmp_path):
    w = _statement_worker(app, settings, tmp_path)
    r = submit(client, app, "AUTO", xlsx_bytes(ROWS), ".xlsx",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "auto-st-1", "key-auto-stmt")
    assert r.status_code == 202, r.text
    job_id = r.json()["jobId"]
    assert w.run_once()
    result = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    assert result["classification"]["type"] == "FINANCIAL_STATEMENT"
    assert "PERIOD_FROM_DOCUMENT" in {w_["code"] for w_ in result["warnings"]}
    lines = [f for f in result["fields"] if f["group"] == "STATEMENT_LINE"]
    assert lines and {f["value"]["asOfDate"] for f in lines} == {"2026-03-31"}
    review = client.get(f"/dps/v1/jobs/{job_id}/review", headers=SERVICE).json()
    decisions = [{"fieldId": f["fieldId"], "action": "CONFIRM"} for f in review["fields"]]
    decisions += [{"fieldId": w_["fieldId"], "action": "ACKNOWLEDGE", "comment": "checked"}
                  for w_ in review["warnings"]]
    done = client.post(f"/dps/v1/jobs/{job_id}/review", headers={**SERVICE, "If-Match": str(review["revision"])},
                       json={"decisions": decisions})
    assert done.status_code == 202 and done.json()["status"] == "DONE", done.text
    figures = client.post("/api/data/values", headers=SERVICE,
                          json={"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]}).json()
    assert figures["points"] and figures["points"][-1]["values"]["EBITDA"] == 5.0


def test_auto_reads_a_compliance_certificate(client, app, settings, tmp_path):
    cert = ("COMPLIANCE CERTIFICATE\nTo: The Lender\nWe certify that for the quarter ended 31 March 2026 the "
            "Borrower complied with the financial covenants:\nDebt Service Coverage Ratio >= 1.25 1.40 Complied\n"
            "Interest Coverage Ratio >= 2.00 2.40 Complied\nSigned: Director")
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "AUTO", text_pdf([cert]), ".pdf", "application/pdf", "auto-cert-1", "key-auto-cert")
    assert w.run_once()
    result = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    assert result["classification"]["type"] == "COMPLIANCE_CERTIFICATE"
    figures = {f["value"]["covenant"]: f["value"]["figure"] for f in result["fields"]
               if f["group"] == "CERTIFIED_FIGURE"}
    assert figures.get("DSCR") == 1.4 and figures.get("ICR") == 2.4


def test_a_picture_is_checked_and_turned_into_one_pdf_page():
    from dps.ingest.inspect import InspectionFailed, declared_format, inspect
    from dps.parse.images import to_pdf
    from dps.parse.page_model import build

    assert declared_format("scan.JPG") == declared_format("x", "image/png") == "IMAGE"
    assert inspect(_png(), "IMAGE").page_count == 1
    with pytest.raises(InspectionFailed) as ex:
        inspect(b"\x89PNG\r\n\x1a\nnot really", "IMAGE")
    assert ex.value.code == "CORRUPT"
    with pytest.raises(InspectionFailed) as ex:
        inspect(b"%PDF-1.4 a pdf named png", "IMAGE")
    assert ex.value.code == "TYPE_SPOOFED"
    assert to_pdf(_png()).startswith(b"%PDF")
    model = build(_png(), "IMAGE", "0" * 64)
    assert model["format"] == "IMAGE" and model["pageCount"] == 1


def test_a_huge_picture_is_refused_before_it_is_decoded():
    from dps.parse.images import ImageRefused, inspect_image

    with pytest.raises(ImageRefused) as ex:
        inspect_image(_png((13_000, 10)))
    assert ex.value.code == "TOO_LARGE"


def test_auto_for_a_type_dps_does_not_read():
    from dps.analyze.classify import Classification

    job = Job(id="ja", document_id="d", job_type="AUTO", tenant_id="default", request_json="{}")
    model = {"format": "PDF", "pages": [{"no": 1, "text": "Deed of hypothecation", "quality": 1.0}]}
    import dps.analyze.classify as classify_mod

    def fake(model, expected, gateway):
        return Classification(type="SECURITY_DOCUMENT", confidence=0.9, method="RULES", scores={})

    real = classify_mod.classify
    classify_mod.classify = fake
    try:
        result = skills.run_job(job, model, None, Tools(), None, "1")
    finally:
        classify_mod.classify = real
    assert result["fields"] == [] and result["status"] == "NEEDS_REVIEW"
    assert result["warnings"][0]["code"] == "TYPE_NOT_READ"


# ---------- 36 publish for AUTO statements; 37 reclassify keeps or sets the date

def test_auto_statement_publishes_like_a_statement():
    from dps.api.review import is_statement

    auto = Job(job_type="AUTO")
    assert is_statement(auto, {"classification": {"type": "FINANCIAL_STATEMENT"}})
    assert not is_statement(auto, {"classification": {"type": "LOAN_AGREEMENT"}})
    assert is_statement(Job(job_type="EXTRACT_STATEMENT"), {})


def test_reclassify_sets_the_period_date(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf([AGREEMENT]), ".pdf", "application/pdf", "rc-1",
               "key-rc-00001")
    assert w.run_once()
    job_id = r.json()["jobId"]
    new = client.post(f"/dps/v1/jobs/{job_id}/reclassify", headers=SERVICE,
                      json={"type": "FINANCIAL_STATEMENT", "reason": "a statement", "asOfDate": "2026-03-31"})
    assert new.status_code == 202, new.text
    with app.state.db.session_factory() as s:
        job = s.get(Job, new.json()["jobId"])
        assert json.loads(job.request_json)["context"]["asOfDate"] == "2026-03-31"
        assert s.get(Document, job.document_id).as_of_date == date(2026, 3, 31)


def test_a_statement_can_be_submitted_without_a_date(client, app):
    data = b"Line item,FY26\nEBITDA,120\n"
    r = submit(client, app, "EXTRACT_STATEMENT", data, ".csv", "text/csv", "st-nodate", "key-st-nodate")
    assert r.status_code == 202, r.text


# ---------- 38 certificates for the bank app's status recommender

def _certificate(app, version, finished, figures, job_type="AUTO", *, cp="CP-7", tenant_id="default",
                 status=jobs.DONE, field_status="CONFIRMED"):
    with app.state.db.session_factory() as s:
        doc = Document(type="COMPLIANCE_CERTIFICATE", file_name="c.pdf", storage_path="x", sha256=version,
                       size_bytes=1, uploaded_by="u")
        s.add(doc)
        s.flush()
        s.add(DocumentCpLink(document_id=doc.id, cp_id=cp, linked_by="u"))
        job = Job(document_id=doc.id, flow="SKILL_PACK", job_type=job_type, status=status, tenant_id=tenant_id,
                  document_version_id=version, finished_at=finished)
        s.add(job)
        s.flush()
        fields = [{"id": f"f{i}", "group": "CERTIFIED_FIGURE", "name": name, "status": field_status,
                   "value": {"covenant": name, "figure": fig, "declaredCompliant": ok}, "source": {"page": 2},
                   "confidence": 0.9} for i, (name, fig, ok) in enumerate(figures)]
        s.add(Run(job_id=job.id, run_no=2, status="DONE", trigger="REVIEW",
                  result_json=json.dumps({"fields": fields, "classification": {"type": "COMPLIANCE_CERTIFICATE"}})))
        s.commit()


def _ask(client, covenants, ids=()):
    body = {"cpId": "CP-7", "certificateFileIds": list(ids),
            "covenants": [{"appliedCovenantId": a, "covenantName": n, "conditionExpression": e}
                          for a, n, e in covenants]}
    r = client.post("/api/v1/ai/covenant-status", headers=SERVICE, json=body)
    assert r.status_code == 200, r.text
    return {x["appliedCovenantId"]: x["recommendedStatus"] for x in r.json()["recommendations"]}


def test_status_comes_from_the_newest_certificate_and_matches_the_apps_measure_codes(client, app):
    from datetime import datetime

    _certificate(app, "cert-old", datetime(2025, 6, 1), [("Interest Coverage Ratio", 1.1, False),
                                                          ("DSCR", 1.4, True)])
    _certificate(app, "cert-new", datetime(2026, 6, 1), [("Interest Coverage Ratio", 2.4, True)])
    asked = [("AC-1", "Interest cover", "INTEREST_COVER >= 2"), ("AC-2", "Minimum DSCR", "DSCR >= 1.25")]
    assert _ask(client, asked) == {"AC-1": "COMPLIED"}  # DSCR only in the old certificate: nothing offered
    assert _ask(client, asked, ["cert-old"]) == {"AC-1": "BREACHED", "AC-2": "COMPLIED"}
    assert _ask(client, asked, ["no-such-certificate"]) == {}
    assert _ask(client, asked, ["", " "]) == {}


def test_certificate_diagnostics_are_opt_in_and_scoped_to_cp_and_tenant(client, app):
    from datetime import datetime

    from test_jobapi import check_def

    finished = datetime(2026, 6, 1)
    figures = [("DSCR", 1.4, True)]
    _certificate(app, "reviewed", finished, figures)
    _certificate(app, "pending", finished, figures, status=jobs.NEEDS_REVIEW)
    _certificate(app, "unapproved", finished, figures, field_status="REJECTED")
    _certificate(app, "another-cp", finished, figures, cp="CP-8")
    _certificate(app, "another-tenant", finished, figures, tenant_id="other")
    requested = ["pending", "reviewed", "missing", "unapproved", "another-cp", "another-tenant", "pending"]
    body = {"cpId": "CP-7", "certificateFileIds": requested,
            "covenants": [{"appliedCovenantId": "AC-1", "covenantName": "DSCR"}]}
    endpoint = "/api/v1/ai/covenant-status"
    default_response = client.post(endpoint, headers=SERVICE, json=body)
    assert default_response.status_code == 200
    assert set(default_response.json()) == {"recommendations"}

    request = {**body, "includeCertificateDiagnostics": True}
    check_def("compat-covenant-status", "request", request)
    response = client.post(endpoint, headers=SERVICE, json=request)
    assert response.status_code == 200
    result = response.json()
    check_def("compat-covenant-status", "response", result)
    assert result["recommendations"] == default_response.json()["recommendations"]
    assert len(result["recommendations"]) == 1
    assert "version reviewed," in result["recommendations"][0]["rationale"]
    assert [item["versionId"] for item in result["certificateDiagnostics"]] == list(dict.fromkeys(requested))
    assert {item["versionId"]: item["status"] for item in result["certificateDiagnostics"]} == {
        "pending": "AWAITING_REVIEW", "reviewed": "REVIEWED_FIGURES_AVAILABLE", "missing": "UNAVAILABLE",
        "unapproved": "NO_REVIEWED_FIGURES", "another-cp": "UNAVAILABLE", "another-tenant": "UNAVAILABLE",
    }
    no_selection = client.post(endpoint, headers=SERVICE, json={**request, "certificateFileIds": []})
    assert no_selection.json()["certificateDiagnostics"] == []


def test_two_figures_that_disagree_give_no_recommendation(client, app):
    from datetime import datetime

    _certificate(app, "cert-two", datetime(2026, 7, 1), [("Historic DSCR", 1.4, True),
                                                          ("Projected DSCR", 1.0, False)], "EXTRACT_CERTIFICATE")
    assert _ask(client, [("AC-9", "DSCR", "DSCR >= 1.2")]) == {}


# ---------- 40 amendments read against the app's formulas

def test_approved_covenants_are_read_from_the_apps_formula():
    base = skills.approved_base([
        {"appliedCovenantId": "A1", "name": "Minimum DSCR", "expression": "DSCR >= 1.25"},
        {"appliedCovenantId": "A2", "name": "Interest cover", "expression": "INTEREST_COVER >= 2"},
        {"appliedCovenantId": "A3", "name": "TNW", "expression": "TANGIBLE_NET_WORTH >= 500"},
        {"appliedCovenantId": "A4", "name": "Equality", "expression": "QUICK_RATIO == 1"},
        {"appliedCovenantId": "A5", "name": "Two parts", "expression": "DSCR >= 1.2 && DEBT_EBITDA <= 3"},
        {"appliedCovenantId": "A6", "name": "Sent", "metric": "ICR", "operator": ">", "threshold": 3.0,
         "expression": "INTEREST_COVER >= 2"},
    ])
    v = {f["id"]: f["value"] for f in base}
    assert (v["A1"]["metric"], v["A1"]["operator"], v["A1"]["threshold"]) == ("DSCR", ">=", 1.25)
    assert v["A2"]["metric"] == "ICR"
    assert (v["A3"]["metric"], v["A3"]["unit"]) == ("MIN_TANGIBLE_NET_WORTH", "INR_CR")
    assert v["A4"]["operator"] == "="
    assert v["A5"]["threshold"] is None and v["A5"]["operator"] is None  # never a guess
    assert (v["A6"]["operator"], v["A6"]["threshold"]) == (">", 3.0)  # what was sent wins


def test_an_unchanged_covenant_is_not_reported_as_changed():
    from dps.skills.amendment_diff.rules import rules

    base = skills.approved_base([{"appliedCovenantId": "A1", "name": "DSCR", "expression": "DSCR >= 1.25"}])
    pages = [{"no": 1, "text": "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 "
                               "times.\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than "
                               "1.25 times."}]
    assert [f for f in rules(pages, {"base": base}) if f["value"].get("metric") == "DSCR"] == []
    changed = [{"no": 1, "text": "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.40 "
                                 "times."}]
    out = [f for f in rules(changed, {"base": base}) if f["value"].get("metric") == "DSCR"]
    assert out and out[0]["value"]["change"]["type"] == "CHANGE"
    assert out[0]["value"]["change"]["from"]["threshold"] == 1.25


def test_amounts_are_compared_in_crore():
    from dps.skills.app_formula import same_threshold

    assert same_threshold({"threshold": 5000, "unit": "million", "currency": "INR"},
                          {"threshold": 500, "unit": "INR_CR", "metric": "MIN_TANGIBLE_NET_WORTH"})
    assert not same_threshold({"threshold": 1.4}, {"threshold": 1.25})


# ---------- 41 waiver extensions

def test_an_extension_of_time_is_read_with_its_new_date():
    from dps.skills.waiver.rules import rules

    pages = [{"no": 1, "text": "The Lender hereby extends the date for delivery of the audited financial "
                               "statements to 31 December 2026. The facility extended to the Borrower remains "
                               "unchanged."}]
    out = rules(pages, {"base": []})
    assert len(out) == 1 and out[0]["value"]["kind"] == "EXTENSION"
    assert out[0]["value"]["extendedTo"] == "2026-12-31"


def test_the_agents_waiver_kind_is_kept_inside_the_contract():
    pack = skills.load("waiver")
    pages = [{"no": 1, "text": "x"}]
    proposals = [{"name": "DSCR", "page": 1, "quote": "x", "value": {"kind": "POSTPONEMENT"}},
                 {"name": "Reporting", "page": 1, "quote": "x",
                  "value": {"kind": "EXTENSION", "extendedTo": "2026-12-31"}}]
    out = skills._agent_only(pack, pages, [], proposals)  # noqa: SLF001
    assert [f["value"]["kind"] for f in out] == ["WAIVER", "EXTENSION"]
    assert out[1]["value"]["extendedTo"] == "2026-12-31"


# ---------- 42 reporting deadlines and the app's frequency

def test_reporting_deadlines_link_to_the_covenants_tested_at_their_frequency():
    from dps.skills.schedule_terms.rules import link_terms

    fields = [
        {"id": "c1", "group": "FINANCIAL_COVENANT", "value": {}, "test": {"frequency": "QUARTERLY"}},
        {"id": "c2", "group": "FINANCIAL_COVENANT", "value": {}, "test": {"frequency": "YEARLY"}},
        {"id": "s1", "group": "SCHEDULE_TERM", "source": {"quote": "a compliance certificate within 45 days of "
                                                                     "the end of each quarter"},
         "value": {"kind": "REPORTING_DEADLINE", "frequency": "QUARTERLY"}},
        {"id": "s2", "group": "SCHEDULE_TERM", "source": {"quote": "insurance certificates within 30 days of "
                                                                     "each half year"},
         "value": {"kind": "REPORTING_DEADLINE", "frequency": "HALF_YEARLY"}},
        {"id": "s3", "group": "SCHEDULE_TERM", "source": {"quote": "tested half yearly"},
         "value": {"kind": "TESTING_CALENDAR", "frequency": "HALF_YEARLY"}},
    ]
    link_terms(fields)
    s1, s2, s3 = (f["value"] for f in fields[2:])
    assert (s1["deliverable"], s1["covenantIds"], s1["appFrequency"]) == ("COMPLIANCE_CERTIFICATE", ["c1"],
                                                                          "QUARTERLY")
    assert (s2["deliverable"], s2["covenantIds"]) == ("INSURANCE", [])
    assert (s3["appFrequency"], s3["appCron"]) == ("CUSTOM", "0 0 2 1 1,7 ?") and "deliverable" not in s3


def test_an_agreement_result_links_its_deadline(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf([AGREEMENT]), ".pdf", "application/pdf", "lk-1",
               "key-link-0001")
    assert w.run_once()
    result = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    deadline = next(f for f in result["fields"] if f["group"] == "SCHEDULE_TERM"
                    and f["value"]["kind"] == "REPORTING_DEADLINE")
    assert deadline["value"]["deliverable"] == "INTERIM_FINANCIAL_STATEMENTS"
    assert deadline["value"]["appFrequency"] == "QUARTERLY"


# ---------- 43 sanction letters: covenant coverage

def test_a_sanction_letter_gets_the_covenant_coverage_check(client, app, settings, tmp_path):
    letter = ("SANCTION LETTER\nSanctioned amount: Rs. 50 crore. Tenor: 60 months.\nFinancial covenants: the Borrower "
              "shall maintain a DSCR of not less than 1.20 times.\nThe Borrower shall ensure that its promoters do "
              "not dilute their shareholding below 51% without the Bank's prior consent.")
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_SANCTION", text_pdf([letter]), ".pdf", "application/pdf", "sl-1",
               "key-sanction-cov")
    assert w.run_once()
    result = client.get(f"/dps/v1/jobs/{r.json()['jobId']}/result", headers=SERVICE).json()
    coverage = result["document"]["coverage"]
    assert coverage["dutySentences"] >= 1 and "ledger" in coverage, coverage  # the whole-document ledger ran
    assert any(f["group"] == "FINANCIAL_COVENANT" and f["value"].get("metric") == "DSCR" for f in result["fields"])


# ---------- 49 the AI budget of long documents

def test_long_documents_get_a_larger_token_budget():
    pack = skills.load("covenants")
    assert skills.token_budget(pack, {"pages": [{}] * 10}) == 700_000
    assert skills.token_budget(pack, {"pages": [{}] * 300}) == 1_500_000
    assert skills.token_budget(pack, {"pages": [{}] * 2000}) == 2_500_000
    assert skills.token_budget(skills.load("sanction"), {"pages": [{}] * 300}) == 80_000


# ---------- 47 no exception text in a job's message

def test_a_crash_leaves_no_exception_text_in_the_job(client, app, settings, tmp_path, monkeypatch):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_AGREEMENT", text_pdf([AGREEMENT]), ".pdf", "application/pdf", "cr-1",
               "key-crash-001")

    def boom(*a, **kw):
        raise ValueError(r"secret C:\internal\path token=abc")

    monkeypatch.setattr(skills, "run_job", boom)
    assert w.run_once()
    job = client.get(f"/dps/v1/jobs/{r.json()['jobId']}", headers=SERVICE).json()
    assert job["errorCode"] == "INTERNAL_ERROR" and "secret" not in (job["error"] or "")
    with app.state.db.session_factory() as s:
        from dps.models import EventFeed

        assert all("secret" not in e.payload_json for e in s.query(EventFeed))
