"""The 6 gaps found when checking DPS against the architect's Miro flows and the Document Upload spec (17 Nov)."""

from __future__ import annotations

import json
from datetime import date

from conftest import h
from test_phase2_ingest import text_pdf
from test_phase3_analyze import model_of
from test_phase4_skills import AMENDMENT, CERTIFICATE, fake_job, make_worker, submit

from dps import jobs
from dps.models import Job
from dps.skills import load
from dps.skills import runner as skills

SERVICE = h("cts-spring", "SERVICE")
BASE = [{"appliedCovenantId": "AC-11", "name": "Debt Service Coverage Ratio", "operator": ">=", "threshold": 1.2,
         "unit": "TIMES"},
        {"appliedCovenantId": "AC-12", "name": "Interest Coverage Ratio", "metric": "ICR", "operator": ">=",
         "threshold": 2.0}]


# ---------- G2: amendment compared with the approved covenants in the CTS DB ----------

def test_amendment_uses_spring_approved_covenants():
    job = fake_job("EXTRACT_AMENDMENT", {"baseCovenants": BASE})
    result = skills.run(load("amendment_diff"), job, model_of(AMENDMENT), None, None)
    changes = {f["value"]["metric"]: f["value"]["change"] for f in result["fields"]}
    assert changes["DSCR"]["type"] == "CHANGE" and changes["DSCR"]["appliedCovenantId"] == "AC-11"
    assert changes["ICR"]["type"] == "DELETE" and changes["ICR"]["appliedCovenantId"] == "AC-12"
    assert changes["CURRENT_RATIO"]["type"] == "ADD" and changes["CURRENT_RATIO"]["appliedCovenantId"] is None
    assert not any(i["code"] == "BASE_FROM_DPS" for f in result["fields"] for i in f["issues"])
    after = {f["value"]["metric"]: f["value"].get("threshold") for f in result["amendedSet"]}
    assert after == {"DSCR": 1.1, "CURRENT_RATIO": 1.25}


def test_amendment_job_accepts_base_covenants(client, app):
    r = submit(client, app, "EXTRACT_AMENDMENT", text_pdf([AMENDMENT]), ".pdf", "application/pdf", "amd-g2",
               "key-amd-g2", baseCovenants=BASE)
    assert r.status_code == 202, r.text
    bad = submit(client, app, "EXTRACT_AMENDMENT", text_pdf([AMENDMENT]), ".pdf", "application/pdf", "amd-g2b",
                 "key-amd-g2b", baseCovenants=[{"name": "no id"}])
    assert bad.status_code == 400


# ---------- G3: issuer and required fields in the upload check ----------

def test_issuer_and_required_fields():
    from dps.analyze.identify import find_identifiers, missing_required

    agreement = model_of("TERM LOAN AGREEMENT\nBETWEEN\nUTKARSH SMALL FINANCE BANK LIMITED, a banking company "
                         "(hereinafter referred to as the \"Lender\")\nAND M/s Shree Polymers Limited",
                         "For and on behalf of the Borrower\nAuthorised Signatory")
    found = find_identifiers(agreement)
    issuer = next(f for f in found if f.kind == "ISSUER")
    assert (issuer.value, issuer.extra["role"]) == ("UTKARSH SMALL FINANCE BANK LIMITED", "LENDER")
    assert issuer.as_result()["role"] == "LENDER"
    assert missing_required("LOAN_AGREEMENT", found) == []
    cert = find_identifiers(model_of("COMPLIANCE CERTIFICATE\nFor ABC & Co\nChartered Accountants"))
    assert next(f for f in cert if f.kind == "ISSUER").extra["role"] == "AUDITOR"
    assert missing_required("COMPLIANCE_CERTIFICATE", cert) == ["PERIOD_END", "UDIN", "SIGNATURE"]
    assert missing_required("OTHER_EVIDENCE", cert) == []


def test_analyze_reports_missing_required_fields():
    from dps.analyze import analysis

    job = fake_job("ANALYZE", {"expectedType": "COMPLIANCE_CERTIFICATE"})
    job.job_type = "ANALYZE"
    result = analysis.analyze(model_of("COMPLIANCE CERTIFICATE\nWe hereby certify compliance.\nFor ABC & Co\n"
                                       "Chartered Accountants"), job)
    warning = next(w for w in result["warnings"] if w["code"] == "REQUIRED_MISSING")
    assert "UDIN" in warning["message"]
    assert analysis.event_payload(result)["missingRequired"] == warning["message"]


# ---------- G4: a restated statement lists what changed ----------


AGREEMENT_TEXT = ("TERM LOAN AGREEMENT\nThis Agreement is made on 1 April 2026 BETWEEN Shree Polymers Limited "
                  "(the Borrower) AND State Bank Limited (the Lender).\n"
                  "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 times, tested "
                  "annually.\nAudited financial statements shall be delivered within 180 days of the end of each "
                  "financial year.\nEvent of Default. Interest Period.")


def test_onboard_job_classifies_then_extracts(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    agr = submit(client, app, "ONBOARD", text_pdf([AGREEMENT_TEXT]), ".pdf", "application/pdf", "onb-1",
                 "key-onb-1", expectedType="LOAN_AGREEMENT").json()["jobId"]
    stmt = submit(client, app, "ONBOARD", text_pdf(["Balance Sheet as at 31 March 2026\nStatement of Profit and "
                                                     "Loss\nRevenue from operations 1,200\nTotal assets 5,000"]),
                  ".pdf", "application/pdf", "onb-2", "key-onb-2").json()["jobId"]
    assert w.run_once() and w.run_once()
    from test_jobapi import check

    res = client.get(f"/dps/v1/jobs/{agr}/result", headers=SERVICE).json()
    check("result", res)
    assert res["classification"]["type"] == "LOAN_AGREEMENT" and res["pipeline"]["skillPack"] == "covenants/3.1.0"
    groups = {f["group"] for f in res["fields"]}
    assert "FINANCIAL_COVENANT" in groups and "SCHEDULE_TERM" in groups
    assert any(i["kind"] == "ISSUER" for i in res["identifiers"])
    other = client.get(f"/dps/v1/jobs/{stmt}/result", headers=SERVICE).json()
    check("result", other)
    assert other["classification"]["type"] == "FINANCIAL_STATEMENT" and other["fields"] == []
    assert other["status"] == "NEEDS_REVIEW" and "TYPE_NOT_ONBOARDING" in {w["code"] for w in other["warnings"]}
    with app.state.db.session_factory() as s:
        assert s.get(Job, stmt).status == jobs.NEEDS_REVIEW


def test_onboard_flags_a_wrong_chosen_type():
    job = fake_job("ONBOARD", {"expectedType": "SANCTION_LETTER"})
    job.job_type = "ONBOARD"
    result = skills.run_job(job, model_of(AGREEMENT_TEXT))
    assert result["classification"]["mismatch"] is True and result["status"] == "NEEDS_REVIEW"
    assert "TYPE_MISMATCH" in result["document"]["reasons"]


# ---------- G5: audit seals in their own folder ----------

def test_seals_go_to_their_own_folder(app, settings, tmp_path):
    from datetime import UTC, datetime

    from dps.models import AuditEvent
    from dps.ops import production as ops

    with app.state.db.session_factory() as s:
        s.add(AuditEvent(actor="u", action="X", target_type="t", target_id="1", detail_json="{}",
                         at=datetime(2026, 1, 1, tzinfo=UTC)))
        s.commit()
        ops.seal_pending(s, tmp_path / "audit-seals", today=date(2026, 1, 2))
    assert (tmp_path / "audit-seals" / "2026-01-01.json").is_file()
    assert not (tmp_path / "derived").exists()


# ---------- G6: optional push of events to Spring ----------

def test_events_are_pushed_to_spring_in_order_at_least_once(client, app, settings):
    import httpx

    from dps.integrations.spring import SpringClient
    from dps.models import PushCursor
    from dps.worker import Worker

    received, state = [], {"down": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["down"]:
            return httpx.Response(503)
        received.extend(json.loads(req.content)["events"])
        return httpx.Response(202)

    core = SpringClient("http://spring", 5, client=httpx.Client(base_url="http://spring",
                                                              transport=httpx.MockTransport(handler)))
    submit(client, app, "EXTRACT_CERTIFICATE", text_pdf([CERTIFICATE]), ".pdf", "application/pdf", "push-1",
           "key-push-1")
    w = Worker(settings, app.state.db.session_factory, core, {})
    assert w.push_events() == 0  # not configured: pull only
    settings.events_push_path = "/api/v1/dps-tools/events"
    assert w.push_events() == 0  # Spring down: nothing lost, cursor unchanged
    with app.state.db.session_factory() as s:
        assert s.get(PushCursor, "spring").seq == 0
    state["down"] = False
    w._push_after = 0
    assert w.push_events() == 1 and received[0]["type"] == "dps.job.accepted"
    assert w.push_events() == 0  # nothing new
    with app.state.db.session_factory() as s:
        assert s.get(PushCursor, "spring").seq == received[-1]["seq"]
