"""Spec "01 - Document Upload", the last DPS gaps: B1 key pages, B2 the document against what its cycle expects
(Flow 3), B3 the MAPPING_REVIEW route, B4 the review backlog for operations (Flow 4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import h
from test_flows import GOOD_ROWS, add_document, database, statement, xlsx_bytes  # noqa: F401 - fixture
from test_phase3_analyze import SAMPLES, model_of
from test_phase4_skills import CERTIFICATE, fake_job

from dps import jobs
from dps.analyze import analysis, obligations
from dps.analyze.identify import Found, find_identifiers
from dps.analyze.keypages import key_pages
from dps.models import Document, Job
from dps.ops.production import review_backlog
from dps.skills import load
from dps.skills import runner as skills

AGREEMENT = """RUPEE TERM LOAN AGREEMENT
This Agreement is made BETWEEN Shree Polymers Ltd (the Borrower) AND State Bank (the Lender).
1. DEFINITIONS AND INTERPRETATION
"EBITDA" means earnings before interest, tax, depreciation and amortisation."""
AGREEMENT_P2 = """21. FINANCIAL COVENANTS
The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 times.
22. INFORMATION UNDERTAKINGS AND COVENANTS AND UNDERTAKINGS
SCHEDULE II Repayment schedule"""
AGREEMENT_P3 = """IN WITNESS WHEREOF
For and on behalf of State Bank
Authorised Signatory"""
STATEMENT = """Shree Polymers Ltd
Standalone Balance Sheet as at 31 March 2026
(All amounts in INR lakhs)
Statement of Profit and Loss for the year ended 31 March 2026
INDEPENDENT AUDITOR'S REPORT"""


# ---------------------------------------------------------------- B1: key pages


def test_key_pages_of_an_agreement_with_page_and_quote():
    pages = {k["role"]: k for k in key_pages(model_of(AGREEMENT, AGREEMENT_P2, AGREEMENT_P3), "LOAN_AGREEMENT")}
    assert pages["PARTIES"]["page"] == 1 and pages["DEFINITIONS"]["page"] == 1
    assert pages["FINANCIAL_COVENANTS"]["page"] == 2 and pages["SCHEDULE"]["page"] == 2
    assert pages["COVENANT_CLAUSE"]["page"] == 2 and pages["SIGNATURE"]["page"] == 3
    assert "FINANCIAL COVENANTS" in pages["FINANCIAL_COVENANTS"]["quote"]


def test_key_pages_of_a_statement_and_nothing_guessed():
    pages = {k["role"]: k["page"] for k in key_pages(model_of(STATEMENT), "FINANCIAL_STATEMENT")}
    assert pages == {"BALANCE_SHEET": 1, "PROFIT_LOSS": 1, "AUDITOR_REPORT": 1}  # no signature line: left out
    assert key_pages(model_of(STATEMENT), "UNKNOWN") == [] and key_pages(model_of(STATEMENT), None) == []


def test_analyze_result_and_event_carry_key_pages():
    job = fake_job("ANALYZE")
    result = analysis.analyze(model_of(SAMPLES["LOAN_AGREEMENT"]), job)
    assert any(k["role"] == "PARTIES" for k in result["keyPages"])
    assert analysis.event_payload(result)["keyPages"][0]["role"] == "PARTIES"


# ---------------------------------------------------------------- B2: what the cycle expects (Flow 3)


def found(model):
    return find_identifiers(model)


def test_each_cycle_expectation_is_checked():
    model = model_of(STATEMENT)
    f = found(model)
    assert obligations.check({}, f, "STANDALONE", model) == []
    ok = {"asOfDate": "2026-03-31", "basis": "STANDALONE", "legalEntity": "Shree Polymers Limited",
          "expectedCurrency": "INR", "expectedUnits": "LAKH"}
    assert obligations.check(ok, f, "STANDALONE", model) == []
    codes = {w["code"]: w for w in obligations.check(
        {"asOfDate": "2025-03-31", "basis": "CONSOLIDATED", "legalEntity": "Other Industries Ltd",
         "expectedCurrency": "USD", "expectedUnits": "CRORE"}, f, "STANDALONE", model)}
    assert set(codes) == set(obligations.CODES)
    assert codes["PERIOD_MISMATCH"]["source"]["page"] == 1 and "2026-03-31" in codes["PERIOD_MISMATCH"]["message"]
    assert obligations.route_for(list(codes.values())) == "MAPPING_REVIEW"
    assert obligations.route_for([codes["UNITS_MISMATCH"]]) == "NEEDS_REVIEW"


def test_entity_by_cin_and_values_not_shown_are_not_guessed():
    f = [Found("CIN", "L17110MH1973PLC019786", 1, "CIN: L17110MH1973PLC019786")]
    assert obligations.check({"legalEntity": "L17110MH1973PLC019786"}, f) == []
    assert obligations.check({"legalEntity": "U17110MH1973PLC019786"}, f)[0]["code"] == "ENTITY_MISMATCH"
    assert obligations.check({"legalEntity": "U17110MH1973PLC019786"}, []) == []  # no CIN shown: not guessed
    assert obligations.check({"legalEntity": "Shree Polymers"}, []) == []  # no name shown
    assert obligations.check({"asOfDate": "2026-03-31", "expectedCurrency": "INR"}, []) == []


def test_analyze_sends_a_wrong_period_to_review_and_a_wrong_entity_to_mapping_review():
    period = analysis.analyze(model_of(STATEMENT), fake_job("ANALYZE", {"asOfDate": "2025-03-31"}))
    assert period["status"] == "NEEDS_REVIEW" and period["document"]["route"] == "NEEDS_REVIEW"
    assert any(r.startswith("PERIOD_MISMATCH") for r in period["document"]["reasons"])
    assert analysis.event_payload(period)["mismatches"] == ["PERIOD_MISMATCH"]
    entity = analysis.analyze(model_of(STATEMENT), fake_job("ANALYZE", {"legalEntity": "Other Industries Ltd"}))
    assert entity["document"]["route"] == "MAPPING_REVIEW"


def test_onboard_checks_the_cycle_too():
    job = fake_job("ONBOARD", {"legalEntity": "Other Industries Ltd"})
    job.job_type = "ONBOARD"
    page = AGREEMENT.replace("State Bank (the Lender)", "State Bank (the Lender) for Shree Polymers Ltd")
    result = skills.run_job(job, model_of(page, AGREEMENT_P2, AGREEMENT_P3))
    assert result["status"] == "NEEDS_REVIEW" and result["document"]["route"] == "MAPPING_REVIEW"
    assert any(k["role"] == "SIGNATURE" for k in result["keyPages"])


def test_certificate_for_another_period_goes_to_review():
    ok = skills.run(load("certificate"), fake_job("EXTRACT_CERTIFICATE", {"asOfDate": "2026-03-31"}),
                    model_of(CERTIFICATE), None, None)
    assert not [w for w in ok["warnings"] if w["code"] == "PERIOD_MISMATCH"]
    wrong = skills.run(load("certificate"), fake_job("EXTRACT_CERTIFICATE", {"asOfDate": "2025-03-31"}),
                       model_of(CERTIFICATE), None, None)
    assert wrong["status"] == "NEEDS_REVIEW" and "PERIOD_MISMATCH" in wrong["document"]["reasons"]


def test_statement_in_other_units_is_held_before_any_value_is_reported(database):  # noqa: F811
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "units.xlsx")
    with database.session_factory() as s:
        s.add(Job(id="j1", document_id=doc_id, flow="SKILL_PACK", job_type="EXTRACT_STATEMENT",
                  request_json=json.dumps({"context": {"expectedUnits": "CRORE"}})))
        s.commit()
    from test_flows import FakeParser, StatementDeps, StatementProcessingFlow

    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(GOOD_ROWS), FakeParser([]), "ds-dps", True)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert any(i.startswith("UNITS_MISMATCH") for i in flow.state.issues)
    with database.session_factory() as s:
        assert s.get(Document, doc_id).status == "NEEDS_REVIEW"


def test_statement_without_cycle_expectations_reports_as_before(database):  # noqa: F811
    flow, _ = statement(database, xlsx_bytes(GOOD_ROWS), "plain.xlsx")
    assert flow.state.outcome == "REPORTED"


# ---------------------------------------------------------------- B4: the review backlog for operations


def test_review_backlog_counts_mapping_problems_and_the_oldest_wait(app, client):
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    with app.state.db.session_factory() as s:
        doc = Document(type="LOAN_AGREEMENT", file_name="a.pdf", storage_path="x", sha256="s", size_bytes=1,
                       uploaded_by="u")
        s.add(doc)
        s.flush()
        for code, hours in (("ENTITY_MISMATCH", 30), ("TYPE_MISMATCH", 2), (None, 1)):
            s.add(Job(document_id=doc.id, flow="SKILL_PACK", status=jobs.NEEDS_REVIEW, error_code=code,
                      finished_at=now - timedelta(hours=hours)))
        s.add(Job(document_id=doc.id, flow="SKILL_PACK", status=jobs.DONE, finished_at=now))
        s.commit()
        backlog = review_backlog(s, now)
    assert backlog["waiting"] == 3 and backlog["mappingReview"] == 2
    assert backlog["byReason"] == {"ENTITY_MISMATCH": 1, "TYPE_MISMATCH": 1, "FIELDS_TO_REVIEW": 1}
    assert backlog["oldestWaitSeconds"] == 30 * 3600
    queues = client.get("/dps/v1/ops/queues", headers=h("support-1", "SUPPORT")).json()
    assert queues["review"]["waiting"] == 3


def test_job_api_takes_the_cycle_expectations(client, app):
    from test_jobapi import request_body, submit

    ok = request_body(app, job_type="EXTRACT_STATEMENT", legalEntity="Shree Polymers Ltd",
                      evidenceType="FINANCIAL_STATEMENT", expectedCurrency="INR", expectedUnits="LAKH")
    r = submit(client, ok, key="key-cycle-0001")
    assert r.status_code in (200, 201, 202), r.text
    bad = request_body(app, job_type="EXTRACT_STATEMENT", version="dv-2", expectedCurrency="rupees")
    assert submit(client, bad, key="key-cycle-0002").status_code in (400, 422)
