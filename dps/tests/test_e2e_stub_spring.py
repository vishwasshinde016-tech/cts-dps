"""The Miro onboarding flow end to end against the stand-in Spring (tests/stub_spring.py): Spring creates the job,
the worker reads it, the Java tools are called through the contract, Spring pulls the result."""

from __future__ import annotations

from stub_spring import StubSpring
from test_architect_gaps import AGREEMENT_TEXT
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase4_skills import SERVICE, submit

from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.integrations.core import CoreClient
from dps.integrations.spring_tools import SpringTools
from dps.parse import sandbox
from dps.skills import runner as skills
from dps.worker import Worker

TEXT = AGREEMENT_TEXT + "\nCorporate Identity Number: L26940MH1990PLC056789"


def worker_with(spring: StubSpring, app, settings, tmp_path) -> Worker:
    tools = SpringTools(CoreClient("http://spring", 5.0, client=spring.client()), retries=0)
    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def pack_runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, None, tools, db, "1", raw))

    return Worker(settings, app.state.db.session_factory, object(), {}, ingestor, {}, None, pack_runner)


def test_onboarding_end_to_end_with_the_java_tools(client, app, settings, tmp_path):
    spring = StubSpring()
    w = worker_with(spring, app, settings, tmp_path)
    job = submit(client, app, "ONBOARD", text_pdf([TEXT]), ".pdf", "application/pdf", "e2e-1", "key-e2e-1",
                 expectedType="LOAN_AGREEMENT").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{job}/result", headers=SERVICE).json()
    check("result", res)
    paths = [p for _, p, _, _ in spring.calls]
    assert "/api/v1/dps-tools/lookups" in paths
    assert "/api/v1/dps-tools/covenant-library" in paths
    assert "/api/v1/dps-tools/formulas/validate" in paths
    dscr = next(f for f in res["fields"] if (f["value"].get("metric") or "") == "DSCR")
    assert dscr["libraryMatch"]["covenantMasterId"] == "CM-DSCR"  # matched in the bank's library (Java)
    assert not [i for i in dscr["issues"] if i["code"] in ("FORMULA_INVALID", "SPRING_UNAVAILABLE")]
    cin = next(i for i in res["identifiers"] if i["kind"] == "CIN")
    assert cin["match"]["status"] == "EXACT"  # the borrower found in master data
    lookups = [c for c in spring.calls if c[1].endswith("/lookups")]
    assert lookups[0][2].get("x-correlation-id")  # every tool call carries the job's correlation id


def test_a_document_of_another_borrower_goes_to_mapping_review(client, app, settings, tmp_path):
    spring = StubSpring()
    w = worker_with(spring, app, settings, tmp_path)
    other = AGREEMENT_TEXT + "\nCorporate Identity Number: U74999MH2015PTC123456"
    job = submit(client, app, "ONBOARD", text_pdf([other]), ".pdf", "application/pdf", "e2e-2", "key-e2e-2",
                 expectedType="LOAN_AGREEMENT").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{job}/result", headers=SERVICE).json()
    assert res["document"]["route"] == "MAPPING_REVIEW"
    assert "MAPPING_CONFLICT" in {w["code"] for w in res["warnings"]}


def test_spring_down_the_job_still_finishes_and_says_so(client, app, settings, tmp_path):
    w = worker_with(StubSpring(down=True), app, settings, tmp_path)
    job = submit(client, app, "ONBOARD", text_pdf([TEXT]), ".pdf", "application/pdf", "e2e-3", "key-e2e-3",
                 expectedType="LOAN_AGREEMENT").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{job}/result", headers=SERVICE).json()
    check("result", res)
    codes = {w["code"] for w in res["warnings"]} | {i["code"] for f in res["fields"] for i in f["issues"]}
    assert "SPRING_UNAVAILABLE" in codes or "LOOKUP_UNAVAILABLE" in codes
    assert res["status"] == "NEEDS_REVIEW"
