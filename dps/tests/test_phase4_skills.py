"""Phase 4: skill packs and the generic flow (sanction, certificate, amendment, schedule terms, statement identities,
commentary), Spring verification, voting, and the jobs end to end."""

from __future__ import annotations

import hashlib
import json

import pytest
from conftest import h
from sqlalchemy import select
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase3_analyze import model_of

from dps import jobs
from dps.agent.gateway import AiUnavailable
from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.integrations.spring_tools import FormulaCheck
from dps.models import Job, Run
from dps.parse import sandbox
from dps.skills import PACKS, for_job_type, load
from dps.skills import runner as skills
from dps.skills.amendment_diff.rules import amended_set
from dps.skills.commentary.rules import check_text, draft, traceable
from dps.skills.common import vote
from dps.worker import Worker

SERVICE = h("cts-spring", "SERVICE")

SANCTION = """Ref No.: HO/CR/2026/11
Dear Sir,
SANCTION LETTER
We are pleased to inform you that the Bank has sanctioned a Term Loan facility.
Sanctioned limit: Rs. 25.50 crore
Tenor: 84 months from the date of first disbursement
Rate of interest: 9.25% p.a. linked to 1 year MCLR plus spread
Primary security: Hypothecation of plant and machinery
Conditions precedent to first disbursement are listed in Annexure A.
The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 times, tested annually.
Repayable in 28 equal quarterly instalments of Rs. 0.91 crore.
Audited financial statements shall be submitted within 180 days of the end of each financial year.
This sanction is valid for 90 days from the date of this letter."""

CERTIFICATE = """COMPLIANCE CERTIFICATE
We hereby certify the financial covenants for the year ended 31 March 2026.
Debt Service Coverage Ratio: required 1.20, actual 1.45 times - Complied
Interest Coverage Ratio: required 2.00, actual 1.80 times - Not complied
For ABC & Co, Chartered Accountants
Name: Ravi Kumar
Partner
Date: 15/05/2026
UDIN: 26123456ABCDEF1234"""

BASE_AGREEMENT = """FACILITY AGREEMENT
21.2 The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 times."""

AMENDMENT = """FIRST AMENDMENT AGREEMENT
With effect from 1 April 2026 the Facility Agreement dated 1 April 2024 is amended as follows.
Clause 21.2 shall be substituted with: The Borrower shall maintain a Debt Service Coverage Ratio of not less than
1.10 times.
Clause 21.5 shall be deleted. Clause 21.5 related to the Interest Coverage Ratio.
A new clause 21.7 shall be inserted: The Borrower shall maintain a Current Ratio of not less than 1.25 times."""


def fake_job(job_type: str, ctx: dict | None = None) -> Job:
    return Job(id="job-1", document_id="doc-1", job_type=job_type, tenant_id="default", sha256="0" * 64,
               document_version_id="v1", request_json=json.dumps({"context": ctx or {}}))


class Tools:
    def __init__(self, up=True, valid=True):
        self.up, self.valid, self.validated = up, valid, []

    def covenant_library(self):
        if not self.up:
            return [], False
        return [{"id": "CM-DSCR", "name": "DSCR", "aliases": ["Debt Service Coverage Ratio"], "metricKeys": ["DSCR"],
                 "expression": None, "active": True}], True

    def lookup_identifiers(self, items, context=None, cid=None):
        from dps.integrations.spring_tools import LookupMatch

        return {i: LookupMatch(i, self.up, "NONE" if self.up else None, (), None if self.up else "SPRING_UNAVAILABLE")
                for i, _, _ in items}

    def validate_formulas(self, items, cid=None):
        self.validated += items
        return {i: FormulaCheck(i, self.up, self.valid if self.up else None,
                                () if self.valid else ({"code": "UNKNOWN_METRIC", "message": "bad"},),
                                None if self.up else "SPRING_UNAVAILABLE") for i, _ in items}


def test_every_pack_loads_and_maps_job_types():
    assert {load(n).name for n in PACKS} == set(PACKS)
    assert for_job_type("EXTRACT_SANCTION").name == "sanction"
    assert for_job_type("EXTRACT_AGREEMENT").name == "covenants"  # one pipeline (WP7): no legacy engine
    assert for_job_type("EXTRACT_STATEMENT").name == "statement_lines"
    assert {load(n).engine for n in PACKS} == {"generic"}
    assert for_job_type("NOPE") is None
    assert load("sanction").risk("SANCTIONED_AMOUNT") == "HIGH" and load("commentary").risk("x") == "LOW"


def test_sanction_pack_reads_terms_covenants_and_schedule():
    tools = Tools()
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), None, tools)
    check("result", result)
    by = {f["name"]: f for f in result["fields"]}
    assert by["SANCTIONED_AMOUNT"]["value"]["amount"] == pytest.approx(255_000_000)
    assert by["SANCTIONED_AMOUNT"]["value"]["currency"] == "INR"
    assert by["FACILITY_TYPE"]["value"]["text"] == "TERM_LOAN" and by["TENOR_MONTHS"]["value"]["amount"] == 84
    assert by["INTEREST_RATE"]["value"]["amount"] == 9.25 and by["VALIDITY"]["value"]["text"].startswith("90 days")
    assert by["SANCTIONED_AMOUNT"]["source"]["blockId"] == "p1b5"
    deadline = by["REPORTING_DEADLINE"]["value"]
    assert (deadline["daysAfterPeriodEnd"], deadline["frequency"]) == (180, "YEARLY")
    assert by["REPAYMENT"]["value"]["instalments"] == 28
    covenant = next(f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT")
    assert covenant["libraryMatch"]["covenantMasterId"] == "CM-DSCR"
    assert ("DSCR >= 1.2" in [e for _, e in tools.validated])
    assert result["pipeline"]["skillPack"] == "sanction/1.0.0"


def test_formula_rejected_or_spring_down_goes_to_review():
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), None, Tools(valid=False))
    cov = next(f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT")
    assert cov["status"] == "NEEDS_REVIEW" and "FORMULA_INVALID" in {i["code"] for i in cov["issues"]}
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), None, Tools(up=False))
    cov = next(f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT")
    assert {i["code"] for i in cov["issues"]} >= {"SPRING_UNAVAILABLE"} and result["status"] == "NEEDS_REVIEW"
    assert "SPRING_UNAVAILABLE" in {w["code"] for w in result["warnings"]}


def test_certificate_pack():
    result = skills.run(load("certificate"), fake_job("EXTRACT_CERTIFICATE"), model_of(CERTIFICATE), None, None)
    check("result", result)
    by = {f["value"]["covenant"]: f["value"] for f in result["fields"]}
    assert by["DSCR"]["figure"] == 1.45 and by["DSCR"]["declaredCompliant"] is True and by["DSCR"]["unit"] == "TIMES"
    assert by["ICR"]["figure"] == 1.8 and by["ICR"]["declaredCompliant"] is False
    assert by["DSCR"]["udin"] == "26123456ABCDEF1234" and by["DSCR"]["signatory"] == "Ravi Kumar"
    assert by["DSCR"]["designation"] == "Partner" and by["DSCR"]["signedOn"] == "2026-05-15"
    assert [w["code"] for w in result["warnings"]] == ["AI_NOT_USED"]  # no model in this test (F12)
    empty = skills.run(load("certificate"), fake_job("EXTRACT_CERTIFICATE"), model_of("nothing here"), None, None)
    assert empty["status"] == "NEEDS_REVIEW" and "NOTHING_FOUND" in empty["document"]["reasons"]


def test_amendment_pack_against_base():
    base = [
        {"group": "FINANCIAL_COVENANT", "name": "DSCR", "value": {"metric": "DSCR", "operator": ">=",
                                                                   "threshold": 1.2}, "id": "b1"},
        {"group": "FINANCIAL_COVENANT", "name": "ICR", "value": {"metric": "ICR", "operator": ">=",
                                                                  "threshold": 2.0}, "id": "b2"},
    ]
    from dps.skills.amendment_diff.rules import rules

    fields = rules(model_of(AMENDMENT)["pages"], {"base": base})
    changes = {f["value"]["metric"]: f["value"]["change"] for f in fields}
    assert changes["DSCR"]["type"] == "CHANGE" and changes["DSCR"]["clause"] == "21.2"
    assert changes["DSCR"]["from"] == {"operator": ">=", "threshold": 1.2}
    assert changes["ICR"]["type"] == "DELETE" and changes["ICR"]["clause"] == "21.5"
    assert changes["CURRENT_RATIO"]["type"] == "ADD"
    assert changes["DSCR"]["effectiveDate"] == "2026-04-01"
    after = {f["value"]["metric"]: f["value"].get("threshold") for f in amended_set(base, fields)}
    assert after == {"DSCR": 1.1, "CURRENT_RATIO": 1.25}
    no_base = rules(model_of(AMENDMENT)["pages"], {})
    assert all(f["status"] == "NEEDS_REVIEW" for f in no_base)
    result = skills.run(load("amendment_diff"), fake_job("EXTRACT_AMENDMENT"), model_of(AMENDMENT), None, None)
    check("result", result)


def test_statement_identities_and_restatement():
    def line(name, amount, period="2026-03-31"):
        return {"group": "STATEMENT_LINE", "name": name, "value": {"amount": amount, "asOfDate": period},
                "issues": [], "status": "AUTO_ACCEPTED"}

    fields = [line("TOTAL_ASSETS", 1000), line("TOTAL_LIABILITIES", 600), line("NET_WORTH", 390), line("PBT", 100),
              line("TAX", 25), line("PAT", 75)]
    warnings = load("statement_lines").checks(fields, {"approved": {("PAT", "2026-03-31"): 80}})
    assert [w["code"] for w in warnings] == ["TOTALS_DO_NOT_ADD"] and "assets" in warnings[0]["message"]
    assert fields[-1]["status"] == "NEEDS_REVIEW" and fields[-1]["issues"][0]["code"] == "RESTATED"
    assert load("statement_lines").rules([], {}) == []


def test_schedule_checks_tenor_and_totals():
    sched = load("schedule_terms")
    text = ("The loan is repayable in 4 equal quarterly instalments of Rs. 10 lakh. The first instalment falls due on "
            "30 June 2031.\nAny breach may be cured within 30 days. Interest shall reset every year.\n"
            "The covenants are tested quarterly.")
    fields = sched.rules(model_of(text)["pages"], {})
    kinds = {f["name"] for f in fields}
    assert kinds == {"REPAYMENT", "CURE_WINDOW", "INTEREST_RESET", "TESTING_CALENDAR"}
    sched.checks(fields, {"sanctionedAmount": 5_000_000, "tenorEnd": "2030-12-31"})
    repay = next(f for f in fields if f["name"] == "REPAYMENT")
    assert {i["code"] for i in repay["issues"]} == {"TOTALS_DO_NOT_ADD", "OUTSIDE_TENOR"}


def test_commentary_from_manifest_only():
    manifest = {"cpName": "Shree Polymers", "period": "FY 2025-26",
                "covenants": [{"name": "DSCR", "result": "COMPLIANT", "actual": 1.45, "operator": ">=",
                               "threshold": 1.2},
                              {"name": "ICR", "result": "BREACHED", "actual": 1.8, "operator": ">=", "threshold": 2}]}
    text = "\n".join(draft(manifest))
    assert traceable(text, manifest) == [] and check_text(text, manifest) == []
    assert check_text("DSCR was 1.9 and the borrower breached ICR", {"covenants": [{"result": "COMPLIANT"}]})
    raw = json.dumps(manifest).encode()
    result = skills.run(load("commentary"), fake_job("DRAFT_COMMENTARY"), None, None, None, raw=raw)
    check("result", result)
    assert result["commentary"]["method"] == "RULES" and result["status"] == "DONE"
    bad = skills.run(load("commentary"), fake_job("DRAFT_COMMENTARY"), None, None, None, raw=b"{not json")
    assert bad["warnings"][0]["code"] == "CORRUPT"


class Gateway:
    """Answers with a fixed list; can be told to fail. Records the routes it was called on."""

    def __init__(self, items=None, error=None):
        self.items, self.error, self.routes = items or [], error, []

    def call_json(self, route, messages, schema, check):
        self.routes.append(route)
        if self.error:
            raise self.error
        answer = {"items": [dict(i) for i in self.items]}
        problem = check(answer)
        if problem:
            from dps.agent.gateway import ModelOutputInvalid

            raise ModelOutputInvalid(problem)
        return answer

    def begin_job(self, limit=None):
        from dps.agent.gateway import JobBudget

        return JobBudget(limit or 1000)

    def end_job(self):
        return None


def test_agent_votes_and_agent_only_fields():
    quote = "Sanctioned limit: Rs. 25.50 crore"
    items = [{"name": "SANCTIONED_AMOUNT", "page": 1, "quote": quote,
              "value": {"key": "SANCTIONED_AMOUNT", "text": "Sanctioned limit: Rs. 25.50 crore", "amount": 255000000.0,
                        "currency": "INR", "date": None}},
             {"name": "TENOR_MONTHS", "page": 1, "quote": "Tenor: 84 months from the date of first disbursement",
              "value": {"key": "TENOR_MONTHS", "amount": 60}}]
    gw = Gateway(items)
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), gw, None)
    by = {f["name"]: f for f in result["fields"] if f["group"] == "AGREEMENT_TERM"}
    assert by["TENOR_MONTHS"]["status"] == "NEEDS_REVIEW"
    assert by["TENOR_MONTHS"]["issues"][0]["code"] == "MODELS_DISAGREE"
    assert gw.routes[0] == "strong" and "challenger" not in gw.routes
    down = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION),
                      Gateway(error=AiUnavailable("x")), None)
    assert down["warnings"][0]["code"] == "AI_UNAVAILABLE"
    ungrounded = Gateway([{**items[0], "quote": "a quote that is not on the page"}])
    assert skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), model_of(SANCTION), ungrounded,
                      None)["warnings"][0]["code"] == "AI_UNGROUNDED"  # the quote was not on the page: dropped
    only = Gateway([{"name": "SECURITY", "page": 1, "quote": "Conditions precedent to first disbursement are listed "
                                                            "in Annexure A.", "value": {"text": "x"}}])
    pages = model_of("Conditions precedent to first disbursement are listed in Annexure A.")
    result = skills.run(load("sanction"), fake_job("EXTRACT_SANCTION"), pages, only, None)
    assert any(f["method"] == "AI" for f in result["fields"])


def test_vote_helper_and_no_pack_has_a_second_model_check():
    fields = [{"name": "A", "value": {"x": 1}, "confidence": 0.8, "issues": [], "status": "AUTO_ACCEPTED"}]
    assert vote(fields, None) == fields
    vote(fields, [{"name": "A", "value": {"x": 1.0}}])
    assert fields[0]["method"] == "RULES+AI_CONFIRMED" and fields[0]["confidence"] == 0.95
    for name in ("sanction", "certificate", "amendment_diff", "covenants", "statement_lines", "schedule_terms"):
        assert "verify" not in load(name).routes  # the challenger was removed (18 Nov 2026)


# ---------- jobs end to end ----------

def submit(client, app, job_type, data, ext, media, version, key, **ctx):
    digest, obj = app.state.store.put(data, ext)
    body = {"jobType": job_type, "context": {"requestedBy": "rm-anil", "cpIds": ["CP-1"], **ctx},
            "document": {"versionId": version, "objectKey": obj, "sha256": digest, "fileName": f"f{ext}",
                         "mediaType": media, "sizeBytes": len(data)}}
    return client.post("/dps/v1/jobs", json=body, headers={**SERVICE, "Idempotency-Key": key})


def make_worker(app, settings, tmp_path):
    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def pack_runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, None, Tools(), db, "1", raw))

    return Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, None, pack_runner)


def test_sanction_and_commentary_jobs_end_to_end(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    r = submit(client, app, "EXTRACT_SANCTION", text_pdf(SANCTION.splitlines() and [SANCTION]), ".pdf",
               "application/pdf", "sv-1", "key-sanction-1")
    assert r.status_code == 202, r.text
    sanction_id = r.json()["jobId"]
    manifest = json.dumps({"cpId": "CP-1", "covenants": [{"name": "DSCR", "result": "COMPLIANT", "actual": 1.4}]})
    r = submit(client, app, "DRAFT_COMMENTARY", manifest.encode(), ".json", "application/json", "mv-1", "key-comm-1")
    assert r.status_code == 202, r.text
    comm_id = r.json()["jobId"]
    assert w.run_once() and w.run_once()
    res = client.get(f"/dps/v1/jobs/{sanction_id}/result", headers=SERVICE).json()
    check("result", res)
    assert any(f["name"] == "SANCTIONED_AMOUNT" for f in res["fields"])
    comm = client.get(f"/dps/v1/jobs/{comm_id}/result", headers=SERVICE).json()
    assert comm["commentary"]["paragraphs"][0].startswith("Covenant review for CP-1")
    with app.state.db.session_factory() as s:
        assert s.get(Job, comm_id).status == jobs.DONE


def test_json_only_for_commentary(client, app):
    r = submit(client, app, "EXTRACT_SANCTION", b"{}", ".json", "application/json", "x-1", "key-json-1")
    assert r.status_code == 400 and r.json()["code"] == "UNSUPPORTED_FORMAT"
    r = submit(client, app, "DRAFT_COMMENTARY", text_pdf(["x"]), ".pdf", "application/pdf", "x-2", "key-json-2")
    assert r.status_code == 400 and r.json()["code"] == "UNSUPPORTED_FORMAT"


def test_amendment_job_reads_base_result(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    base = submit(client, app, "EXTRACT_AGREEMENT", text_pdf([BASE_AGREEMENT]), ".pdf", "application/pdf", "agr-1",
                  "key-agr-base").json()["jobId"]
    assert w.run_once()  # the base agreement's result is the run the skills flow saved
    amend = submit(client, app, "EXTRACT_AMENDMENT", text_pdf([AMENDMENT]), ".pdf", "application/pdf", "amd-1",
                   "key-amd-1", amendsVersionId="agr-1").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{amend}/result", headers=SERVICE).json()
    check("result", res)
    change = {f["value"]["metric"]: f["value"]["change"]["type"] for f in res["fields"]}
    assert change["DSCR"] == "CHANGE" and change["CURRENT_RATIO"] == "ADD"
    assert {f["value"]["metric"] for f in res["amendedSet"]} == {"DSCR", "CURRENT_RATIO"}
    with app.state.db.session_factory() as db:
        base_run = db.scalars(select(Run).where(Run.job_id == base)).one()
        amendment_run = db.scalars(select(Run).where(Run.job_id == amend)).one()
        assert amendment_run.base_run_id == base_run.id
        assert amendment_run.attempt_no == 1


def test_reporting_deadlines_handle_each_of_its_financial_half_years():
    from dps.skills.schedule_terms.rules import link_terms

    page = ("Deliver within 180 days after the end of each of its financial years, the audited consolidated "
            "financial statements.\nDeliver within 30 days after the end of each of its financial half years, "
            "the unaudited consolidated financial\nstatements.")
    fields = load("schedule_terms").rules([{"no": 1, "text": page}], {})
    link_terms(fields)
    assert [(f["value"]["frequency"], f["value"]["daysAfterPeriodEnd"], f["value"]["deliverable"])
            for f in fields] == [("YEARLY", 180, "AUDITED_FINANCIAL_STATEMENTS"),
                                ("HALF_YEARLY", 30, "INTERIM_FINANCIAL_STATEMENTS")]


def test_an_agreement_job_result_gets_schedule_terms(client, app, settings, tmp_path):
    from dps.ingest.pipeline import Ingestor as Ing

    settings.derived_dir = tmp_path / "derived"
    store = LocalStore(settings.storage_dir, tmp_path / "staging", settings.derived_dir)
    data = text_pdf(["FACILITY AGREEMENT\nThe audited accounts shall be delivered within 120 days of the end of "
                     "each financial year."])
    job_id = submit(client, app, "EXTRACT_AGREEMENT", data, ".pdf", "application/pdf", "agr-9", "key-agr-9").json()[
        "jobId"]
    ing = Ing(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, None, Tools(), db, "1", raw))

    page_models: dict = {}
    Worker(settings, app.state.db.session_factory, object(), {}, ing, page_models, pack_runner=runner).run_once()
    res = client.get(f"/dps/v1/jobs/{job_id}/result", headers=SERVICE).json()
    check("result", res)
    sched = [f for f in res["fields"] if f["group"] == "SCHEDULE_TERM"]
    assert sched and sched[0]["value"]["daysAfterPeriodEnd"] == 120
    assert hashlib.sha256(data).hexdigest() == res["sha256"]


# ---------- evidence and ops endpoints ----------

def test_page_image_runs_ask_and_data_values(client, app, settings, tmp_path):
    settings.derived_dir = tmp_path / "derived"
    store = LocalStore(settings.storage_dir, tmp_path / "staging", settings.derived_dir)
    data = text_pdf([SANCTION])
    job_id = submit(client, app, "EXTRACT_SANCTION", data, ".pdf", "application/pdf", "sv-9", "key-ev-1").json()[
        "jobId"]
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def pack_runner(db, job, model, raw):
        skills.finish(db, job, skills.run(skills.pack_for(job.job_type), job, model, None, None, db, "1", raw))

    Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, None, pack_runner).run_once()
    r = client.get("/dps/v1/versions/sv-9/pages/1/image", headers=SERVICE)
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get("/dps/v1/versions/sv-9/pages/1/image", headers=SERVICE).content == r.content  # write-once
    assert client.get("/dps/v1/versions/sv-9/pages/9/image", headers=SERVICE).status_code == 404
    assert client.get("/dps/v1/versions/nope/pages/1/image", headers=SERVICE).status_code == 404
    runs = client.get("/dps/v1/versions/sv-9/runs", headers=SERVICE).json()
    assert runs["runs"][0]["jobId"] == job_id and runs["runs"][0]["skillPack"] == "sanction/1.0.0"
    answer = client.post("/dps/v1/versions/sv-9/ask", json={"question": "What is the tenor of the loan?"},
                         headers=SERVICE).json()
    assert answer["citations"] and answer["citations"][0]["page"] == 1 and answer["grounded"] is True
    usage = client.get("/dps/v1/ops/usage", headers=h("ops-1", "SUPPORT")).json()
    assert usage["rows"] == [] and usage["to"] >= usage["from"]
    assert client.get("/dps/v1/ops/usage", headers=SERVICE).status_code == 403
    values = client.post("/api/data/values", json={"cpId": "CP-1", "to": "2026-03-31", "keys": ["EBITDA"]},
                         headers=SERVICE).json()
    assert values == {"cpId": "CP-1", "points": [], "missingKeys": ["EBITDA"], "sourceChangedAt": None}
