"""Full review WP6 (company, facility, condition on each covenant) and WP9 (waivers, guarantees, amended and
restated agreements, document sets). The IDs are the finding IDs in FIX-PLAN.md."""

from __future__ import annotations

import json

from conftest import h
from test_jobapi import check
from test_phase2_ingest import text_pdf
from test_phase3_analyze import model_of
from test_phase4_skills import AMENDMENT, fake_job, make_worker, submit

from dps.analyze.classify import classify
from dps.skills import load
from dps.skills import runner as skills
from dps.skills.covenants import obligation
from dps.skills.reconcile_set.rules import parse_manifest, reconcile

SERVICE = h("cts-spring", "SERVICE")
BASE = [{"appliedCovenantId": "AC-11", "name": "Debt Service Coverage Ratio", "metric": "DSCR", "operator": ">=",
         "threshold": 1.2, "unit": "TIMES", "clause": "21.2"},
        {"appliedCovenantId": "AC-12", "name": "Interest Coverage Ratio", "metric": "ICR", "operator": ">=",
         "threshold": 2.0, "clause": "21.5"}]
TWO_COMPANIES = ("21. Financial Covenants\n"
                 "21.1 The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times on a "
                 "standalone basis.\n"
                 "21.2 The Guarantor shall maintain a Debt Service Coverage Ratio of not less than 1.10 times, tested "
                 "on a consolidated basis.\n")
SPRINGING = ("7.1 Fixed Charge Coverage Ratio\nIf Excess Availability is less than USD 10,000,000 on any day, the "
             "Borrower shall maintain a Fixed Charge Coverage Ratio of at least 1.10:1.\n")
WAIVER = ("WAIVER LETTER\nTo: Shree Polymers Limited\nRe: Facility Agreement dated 1 April 2024\n"
          "You have informed us that the Debt Service Coverage Ratio under Clause 21.2 was not met for the quarter "
          "ending 31 March 2026.\n"
          "The Lender hereby waives the breach of Clause 21.2 (Debt Service Coverage Ratio) for the quarter ending "
          "31 March 2026, subject to the Borrower infusing equity of INR 10 crore by 30 June 2026.\n"
          "This waiver shall expire on 30 September 2026. Clause 21.2 shall again apply from the next test date.\n"
          "This letter does not constitute a waiver of any other provision of the Facility Agreement.\n"
          "For and on behalf of State Bank Limited\nAuthorised Signatory")
GUARANTEE = ("DEED OF GUARANTEE\nThis Deed of Guarantee is made by Shree Holdings Limited (the Guarantor) in favour "
             "of State Bank Limited (the Lender).\nThe Guarantor hereby irrevocably and unconditionally guarantees "
             "the obligations of Shree Polymers Limited.\n"
             "The Guarantor shall maintain a Tangible Net Worth of not less than INR 500 crore on a consolidated "
             "basis.\nFor and on behalf of Shree Holdings Limited\nAuthorised Signatory")
RESTATED = ("AMENDED AND RESTATED TERM LOAN AGREEMENT\nThis Agreement amends and restates the Term Loan Agreement "
            "dated 1 April 2024 BETWEEN Shree Polymers Limited (the Borrower) AND State Bank Limited (the Lender).\n"
            "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.30 times, tested annually."
            "\nEvent of Default. Interest Period.\nFor and on behalf of State Bank Limited\nAuthorised Signatory")


def _covenants(text: str, ctx: dict | None = None) -> list[dict]:
    result = skills.run(load("covenants"), fake_job("ONBOARD", ctx), model_of(text), None, None)
    return [f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT"]


# ---------- WP6: who, on whose figures, for which facility, when ----------

def test_f4_the_borrowers_and_the_guarantors_covenants_stay_apart():
    dscr = [f for f in _covenants(TWO_COMPANIES) if f["value"]["metric"] == "DSCR"]
    scopes = {(f["value"]["scope"]["obligor"], f["value"]["scope"]["basis"], f["value"]["threshold"]) for f in dscr}
    assert ("Borrower", "STANDALONE", 1.25) in scopes and ("Guarantor", "CONSOLIDATED", 1.1) in scopes
    assert all(f["value"]["scope"]["confidence"] == "STATED" for f in dscr)


def test_f3_a_springing_covenant_records_its_trigger_and_goes_to_a_person():
    [fccr] = [f for f in _covenants(SPRINGING) if f["value"]["metric"] != "OTHER"][:1]
    [condition] = fccr["value"]["conditions"]
    assert condition["kind"] == "SPRINGING" and condition["metric"] == "EXCESS_AVAILABILITY"
    assert condition["operator"] == "<" and condition["threshold"] == 10_000_000 and condition["unit"] == "USD"
    assert fccr["status"] == "NEEDS_REVIEW" and "CONDITIONAL" in {i["code"] for i in fccr["issues"]}


def test_f4_a_covenant_that_names_no_company_goes_to_a_person():
    f = {"group": "FINANCIAL_COVENANT", "value": {"metric": "DSCR", "threshold": 1.2},
         "source": {"quote": "DSCR of not less than 1.2 times"}, "test": None, "issues": [], "status": "AUTO_ACCEPTED"}
    obligation.enrich([f])
    assert f["value"]["scope"] == {"obligor": None, "basis": None, "facilityRefs": [], "confidence": "UNKNOWN"}
    assert f["status"] == "NEEDS_REVIEW" and "SCOPE_UNKNOWN" in {i["code"] for i in f["issues"]}


def test_f4_one_clause_for_two_companies_is_flagged_and_the_facility_kept():
    f = {"group": "FINANCIAL_COVENANT", "value": {"metric": "DSCR", "threshold": 1.2},
         "source": {"quote": "The Borrower and the Guarantor shall each maintain, in respect of Facility B, a DSCR "
                             "of not less than 1.2, commencing with the financial quarter ending 31 March 2027."},
         "test": None, "issues": [], "status": "AUTO_ACCEPTED"}
    obligation.enrich([f])
    assert f["value"]["scope"]["obligor"] == "Borrower; Guarantor"
    assert f["value"]["scope"]["facilityRefs"] == ["Facility B"]
    assert f["test"]["firstTestDate"] == "2027-03-31"
    assert "SCOPE_SEVERAL" in {i["code"] for i in f["issues"]}


def test_g27_holiday_baskets_and_equity_cure_are_read_never_invented():
    pages = [{"no": 9, "text": "20.4 Equity Cure. The Parent may cure a breach by a Cure Amount, provided that no "
                               "more than two times in any four consecutive Financial Quarters. 20.5 Other."}]
    fin = {"group": "FINANCIAL_COVENANT", "value": {"metric": "LEVERAGE", "threshold": 3.5},
           "source": {"quote": "The Borrower shall ensure Leverage does not exceed 3.5x; this covenant shall not be "
                               "tested until 30 June 2026."}, "test": None, "issues": [], "status": "AUTO_ACCEPTED"}
    neg = {"group": "NON_FINANCIAL_COVENANT", "value": {"metric": "NEG_DEBT", "threshold": None},
           "source": {"quote": "No member of the Group may incur Financial Indebtedness other than Permitted "
                               "Indebtedness not exceeding USD 5,000,000 in aggregate."},
           "test": None, "issues": [], "status": "AUTO_ACCEPTED"}
    obligation.enrich([fin, neg], pages)
    assert fin["test"]["holiday"]["until"] == "2026-06-30"
    assert fin["value"]["equityCure"]["allowed"] and fin["value"]["equityCure"]["maxUses"] == 2
    assert neg["value"]["scope"]["obligor"] == "Group"
    assert neg["value"]["baskets"] == [{"text": "not exceeding USD 5,000,000", "amount": 5_000_000.0,
                                        "currency": "USD"}]
    assert "equityCure" not in neg["value"]  # a cure right is for financial covenants
    for trigger in ("so long as any Loan is outstanding", "if any are requested", "as if the Loan were drawn"):
        f = {"group": "FINANCIAL_COVENANT", "value": {"metric": "DSCR", "threshold": 1.2},
             "source": {"quote": f"The Borrower shall maintain DSCR of 1.2 {trigger}."}, "issues": [],
             "status": "AUTO_ACCEPTED"}
        obligation.enrich([f])
        assert f["value"]["conditions"] == [], trigger  # the life of the loan is not a trigger


def test_g27_the_agents_company_is_used_only_when_the_quote_names_none():
    f = {"group": "FINANCIAL_COVENANT", "value": {"metric": "DSCR", "threshold": 1.2,
                                                  "_ai": {"obligor": "Guarantors", "facilityRefs": ["Facility A"],
                                                          "conditions": [{"text": "invented by the model"}]}},
         "source": {"quote": "DSCR shall be at least 1.2"}, "test": None, "issues": [], "status": "AUTO_ACCEPTED"}
    obligation.enrich([f])
    assert f["value"]["scope"] == {"obligor": "Guarantor", "basis": None, "facilityRefs": ["Facility A"],
                                   "confidence": "INFERRED"}
    assert f["value"]["conditions"] == [] and "_ai" not in f["value"]  # a condition not in the quote is dropped


def test_g27_a_reviewed_covenant_keeps_the_persons_decision():
    f = {"group": "FINANCIAL_COVENANT", "value": {"metric": "FCCR", "threshold": 1.1},
         "source": {"quote": SPRINGING.splitlines()[1]}, "test": None, "issues": [], "status": "CONFIRMED"}
    obligation.enrich([f])
    assert f["status"] == "CONFIRMED" and f["issues"] == [] and f["value"]["conditions"]


def test_g27_the_vote_hands_the_agents_company_to_the_rules_field():
    from dps.skills.common import vote

    fields = [{"name": "DSCR", "value": {"metric": "DSCR", "threshold": 1.2}, "issues": [], "confidence": 0.8}]
    vote(fields, [{"name": "DSCR", "value": {"metric": "DSCR", "threshold": 1.2, "obligor": "Borrower",
                                             "facilityRefs": []}, "page": 1}])
    assert fields[0]["value"]["_ai"] == {"obligor": "Borrower"}


# ---------- WP9: document types ----------

def test_f10_waiver_guarantee_and_restated_agreement_have_their_own_types():
    assert classify(model_of(WAIVER)).type == "WAIVER_CONSENT"
    assert classify(model_of(GUARANTEE)).type == "GUARANTEE"
    assert classify(model_of(RESTATED)).type == "AMENDED_AND_RESTATED"
    us = model_of('FIRST AMENDMENT TO SECOND AMENDED AND RESTATED CREDIT AGREEMENT (this "Amendment")\n'
                  "Section 7.1 shall be amended and restated in its entirety.")
    assert classify(us).type == "AMENDMENT"  # an amendment of an A&R agreement is an amendment
    assert not classify(model_of(RESTATED), "LOAN_AGREEMENT").mismatch  # an A&R agreement is a loan agreement


def test_f10_a_facility_agreement_with_a_guarantee_clause_is_a_loan_agreement():
    """Audit 20 Nov 2026: the architect's UK senior facilities agreement read as GUARANTEE, because LMA agreements list
    "Guarantee and indemnity" in their contents. The facility agreement's title wins."""
    sfa = model_of("FACILITIES AGREEMENT\nDated July 2023\nfor AQUILA MIDCO 2 LIMITED as Parent",
                   "TABLE OF CONTENTS\n1 Definitions and interpretation ....... 6\n"
                   "23 Guarantee and indemnity ....... 160\n24 Representations ....... 170")
    assert classify(sfa).type == "LOAN_AGREEMENT"
    assert classify(model_of(GUARANTEE)).type == "GUARANTEE"


# ---------- WP9: waivers ----------

def test_f10_a_waiver_names_the_approved_covenant_the_period_conditions_and_expiry():
    job = fake_job("EXTRACT_WAIVER", {"baseCovenants": BASE})
    result = skills.run(load("waiver"), job, model_of(WAIVER), None, None)
    [w] = [f for f in result["fields"] if f["group"] == "WAIVER"]
    v = w["value"]
    assert v["kind"] == "WAIVER" and v["target"]["appliedCovenantId"] == "AC-11" and v["target"]["clause"] == "21.2"
    assert v["periods"] == [{"from": None, "to": None, "testDate": "2026-03-31"}]
    assert any("infusing equity" in c for c in v["conditions"])
    assert v["expiresOn"] == "2026-09-30" and "next test date" in v["reinstatement"]
    assert w["status"] == "NEEDS_REVIEW" and result["status"] == "NEEDS_REVIEW"
    check("result", result)


def test_f10_a_waiver_without_the_approved_covenants_is_blocked():
    result = skills.run(load("waiver"), fake_job("EXTRACT_WAIVER"), model_of(WAIVER), None, None)
    assert "BASE_MISSING" in {w["code"] for w in result["warnings"]}
    assert result["document"]["route"] == "NEEDS_REVIEW"
    [w] = [f for f in result["fields"] if f["group"] == "WAIVER"]
    assert "WAIVER_TARGET_UNKNOWN" in {i["code"] for i in w["issues"]}


def test_g18_amendment_and_waiver_jobs_need_their_base(client, app):
    pdf = text_pdf([AMENDMENT])
    r = submit(client, app, "EXTRACT_WAIVER", pdf, ".pdf", "application/pdf", "wv-0", "key-wv-0000")
    assert r.status_code == 400 and r.json()["code"] == "BASE_REQUIRED"
    r = submit(client, app, "EXTRACT_AMENDMENT", pdf, ".pdf", "application/pdf", "am-0", "key-am-0000")
    assert r.status_code == 400 and r.json()["code"] == "BASE_REQUIRED"
    ok = submit(client, app, "EXTRACT_AMENDMENT", pdf, ".pdf", "application/pdf", "am-1", "key-am-0001",
                amendsVersionId="agr-1")
    assert ok.status_code == 202, ok.text


def test_g18_an_amendment_finds_the_base_of_an_onboard_agreement(client, app, settings, tmp_path):
    from test_architect_gaps import AGREEMENT_TEXT

    w = make_worker(app, settings, tmp_path)
    submit(client, app, "ONBOARD", text_pdf([AGREEMENT_TEXT]), ".pdf", "application/pdf", "agr-g18", "key-g18-agr")
    assert w.run_once()
    amd = submit(client, app, "EXTRACT_AMENDMENT", text_pdf([AMENDMENT]), ".pdf", "application/pdf", "amd-g18",
                 "key-g18-amd", amendsVersionId="agr-g18").json()["jobId"]
    assert w.run_once()
    res = client.get(f"/dps/v1/jobs/{amd}/result", headers=SERVICE).json()
    issues = {i["code"] for f in res["fields"] for i in f["issues"]}
    assert "BASE_UNKNOWN" not in issues and "BASE_FROM_DPS" in issues


# ---------- WP9: end to end through ONBOARD and the job API ----------

def test_f10_onboard_reads_a_waiver_a_guarantee_and_a_restated_agreement(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    ids = {name: submit(client, app, "ONBOARD", text_pdf([text]), ".pdf", "application/pdf", f"onb-{name}",
                        f"key-onb-{name}", baseCovenants=BASE, restatesVersionId="agr-2024").json()["jobId"]
           for name, text in (("waiver", WAIVER), ("guarantee", GUARANTEE), ("restated", RESTATED))}
    for _ in ids:
        assert w.run_once()
    res = {name: client.get(f"/dps/v1/jobs/{i}/result", headers=SERVICE).json() for name, i in ids.items()}
    for r in res.values():
        check("result", r)
    assert res["waiver"]["classification"]["type"] == "WAIVER_CONSENT"
    assert [f["value"]["target"]["appliedCovenantId"] for f in res["waiver"]["fields"]
            if f["group"] == "WAIVER"] == ["AC-11"]
    tnw = [f for f in res["guarantee"]["fields"] if f["group"] == "FINANCIAL_COVENANT"]
    assert tnw and tnw[0]["value"]["scope"]["obligor"] == "Guarantor"
    assert res["restated"]["restates"]["versionId"] == "agr-2024"
    assert "RESTATES" in {w["code"] for w in res["restated"]["warnings"]}


def _done(client, app, w, job_type, text, version, **ctx):
    job_id = submit(client, app, job_type, text_pdf([text]), ".pdf", "application/pdf", version, f"key-{version}",
                    **ctx).json()["jobId"]
    assert w.run_once()
    return job_id


def test_f10_reconcile_set_gives_the_covenants_in_force_on_a_date(client, app, settings, tmp_path):
    w = make_worker(app, settings, tmp_path)
    _done(client, app, w, "EXTRACT_AMENDMENT", AMENDMENT, "set-amd", baseCovenants=BASE)
    _done(client, app, w, "EXTRACT_WAIVER", WAIVER, "set-wv", baseCovenants=BASE)
    manifest = {"setId": "SET-1", "asOfDate": "2026-03-31", "documents": [
        {"versionId": "set-agr", "role": "AGREEMENT", "effectiveDate": "2024-04-01"},
        {"versionId": "set-amd", "role": "AMENDMENT", "effectiveDate": "2026-04-01"},
        {"versionId": "set-wv", "role": "WAIVER", "effectiveDate": "2026-05-15"},
        {"versionId": "set-later", "role": "AMENDMENT", "effectiveDate": "2027-01-01"}]}
    for as_of, key in (("2026-03-31", "set-1"), ("2026-06-30", "set-2")):
        manifest["asOfDate"] = as_of
        raw = json.dumps(manifest).encode()
        job = submit(client, app, "RECONCILE_SET", raw, ".json", "application/json", key, f"key-{key}",
                     baseCovenants=BASE)
        assert job.status_code == 202, job.text
        assert w.run_once()
        res = client.get(f"/dps/v1/jobs/{job.json()['jobId']}/result", headers=SERVICE).json()
        check("result", res)
        by_id = {f["value"].get("appliedCovenantId"): f for f in res["fields"]}
        later = ["set-amd", "set-wv", "set-later"] if as_of == "2026-03-31" else ["set-later"]
        assert res["set"]["notYetEffective"] == later and res["set"]["missing"] == []
        assert all(f["status"] == "NEEDS_REVIEW" for f in res["fields"])
        if as_of == "2026-03-31":  # the amendment and the waiver are not yet effective
            assert by_id["AC-11"]["value"]["threshold"] == 1.2 and "AC-12" in by_id
        else:  # DSCR changed to 1.10, ICR deleted, current ratio added, the waiver is attached
            dscr = by_id["AC-11"]
            assert dscr["value"]["threshold"] == 1.1 and "AC-12" not in by_id
            assert [t["action"] for t in dscr["value"]["inForce"]["trail"]] == ["APPROVED", "CHANGED", "WAIVED"]
            [waiver] = dscr["value"]["inForce"]["waivers"]
            assert waiver["periods"][0]["testDate"] == "2026-03-31" and not waiver["activeAtDate"]
            assert any(f["value"]["metric"] == "CURRENT_RATIO" for f in res["fields"])


def test_f10_a_set_with_a_missing_document_or_a_bad_manifest_is_blocked():
    manifest = {"asOfDate": "2026-03-31", "documents": [{"versionId": "x", "role": "AMENDMENT"}]}
    fields, warnings, report = reconcile(manifest, {"x": None}, skills.approved_base(BASE))
    assert report["missing"] == ["x"] and "SET_INCOMPLETE" in {w["code"] for w in warnings}
    assert len(fields) == 2
    assert parse_manifest(b"{not json")[0] is None
    assert parse_manifest(b'{"asOfDate": "2026-03-31", "documents": [{"versionId": "x", "role": "MEMO"}]}')[0] is None
    result = skills.run(load("reconcile_set"), fake_job("RECONCILE_SET"), None, None, None, raw=b"[]")
    assert result["status"] == "NEEDS_REVIEW" and "SET_MANIFEST_INVALID" in {w["code"] for w in result["warnings"]}


def test_f10_a_restated_agreement_replaces_the_set_and_a_waiver_applies_at_its_date():
    restated = {"jobId": "j-ar", "fields": [
        {"id": "f1", "group": "FINANCIAL_COVENANT", "name": "DSCR", "status": "NEEDS_REVIEW", "issues": [],
         "value": {"metric": "DSCR", "threshold": 1.3}}]}
    waiver = {"jobId": "j-w", "fields": [
        {"id": "w1", "group": "WAIVER", "name": "DSCR", "status": "CONFIRMED", "issues": [],
         "value": {"kind": "WAIVER", "target": {"metric": "DSCR"}, "conditions": [], "expiresOn": None,
                   "periods": [{"from": None, "to": None, "testDate": "2026-03-31"}]}}]}
    manifest = {"asOfDate": "2026-03-31", "documents": [
        {"versionId": "ar", "role": "AMENDED_AND_RESTATED", "effectiveDate": "2025-01-01"},
        {"versionId": "w", "role": "WAIVER", "effectiveDate": "2026-03-01"}]}
    fields, warnings, _ = reconcile(manifest, {"ar": restated, "w": waiver}, skills.approved_base(BASE))
    [dscr] = fields
    assert dscr["value"]["threshold"] == 1.3 and dscr["value"]["inForce"]["waivedAtDate"]
    assert [t["action"] for t in dscr["value"]["inForce"]["trail"]] == ["RESTATED", "WAIVED"]
    assert warnings == []
