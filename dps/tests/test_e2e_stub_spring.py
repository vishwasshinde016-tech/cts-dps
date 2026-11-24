"""The Miro onboarding flow end to end against the stand-in Spring (tests/stub_spring.py): Spring creates the job,
the worker reads it, the Java tools are called through the contract, Spring pulls the result."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from stub_spring import StubSpring
from test_architect_gaps import AGREEMENT_TEXT
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase4_skills import CERTIFICATE, SANCTION, SERVICE, submit

from dps.agent.gateway import from_settings
from dps.ingest.pipeline import Ingestor
from dps.ingest.store import LocalStore
from dps.integrations.spring import SpringClient
from dps.integrations.spring_tools import SpringTools
from dps.models import AiCall
from dps.parse import sandbox
from dps.settings import Settings, export_model_env
from dps.skills import runner as skills
from dps.worker import Worker

TEXT = AGREEMENT_TEXT + "\nCorporate Identity Number: L26940MH1990PLC056789"
LIVE_ENV_FILE = os.getenv("DPS_LIVE_AZURE_ENV")


def worker_with(spring: StubSpring, app, settings, tmp_path, gateway=None) -> Worker:
    tools = SpringTools(SpringClient("http://spring", 5.0, client=spring.client()), retries=0)
    store = LocalStore(settings.storage_dir, tmp_path / "staging", tmp_path / "derived")
    ingestor = Ingestor(store, "1", lambda req, hint: sandbox.parse_in_process(
        sandbox.ParseRequest(req.data, req.fmt, req.sha256, req.version_id, False)), ocr=False)

    def pack_runner(db, job, model, raw):
        skills.finish(db, job, skills.run_job(job, model, gateway, tools, db, "1", raw))

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


@pytest.mark.skipif(not LIVE_ENV_FILE, reason="opt-in live model check on synthetic PDFs")
@pytest.mark.parametrize(("doc_type", "text"), [
    ("LOAN_AGREEMENT", TEXT),
    ("LOAN_AGREEMENT", TEXT + "\nThe Borrower shall maintain fire insurance during the facility tenor."),
    ("SANCTION_LETTER", SANCTION),
    ("COMPLIANCE_CERTIFICATE", CERTIFICATE),
], ids=("agreement", "agreement-insurance", "sanction", "certificate"))
def test_live_azure_auto_against_standin_spring(client, app, settings, tmp_path, doc_type, text):
    export_model_env(LIVE_ENV_FILE)
    model_settings = Settings(_env_file=LIVE_ENV_FILE, ai_parallel_reads=4)
    gateway = from_settings(model_settings, app.state.db.session_factory)
    spring = StubSpring()
    worker = worker_with(spring, app, settings, tmp_path, gateway)
    job = submit(client, app, "AUTO", text_pdf([text]), ".pdf", "application/pdf", "live-auto-1",
                 "live-auto-synthetic").json()["jobId"]
    assert worker.run_once()
    result = client.get(f"/dps/v1/jobs/{job}/result", headers=SERVICE).json()
    check("result", result)
    assert result["classification"]["type"] == doc_type
    assert result["fields"]
    with app.state.db.session_factory() as db:
        assert db.scalar(select(AiCall).where(AiCall.job_id == job, AiCall.status == "OK",
                                              AiCall.prompt_tokens > 0).limit(1)) is not None
    if doc_type == "LOAN_AGREEMENT":
        paths = {p for _, p, _, _ in spring.calls}
        assert {"/api/v1/dps-tools/covenant-library", "/api/v1/dps-tools/formulas/validate"} <= paths
