from test_jobapi import check
from test_phase4_skills import fake_job

from dps.skills.runner import run_job


def test_numbered_amendment_provisions_and_allocations_stay_out_of_covenant_set():
    pages = [
        {"no": 1, "text": "We propose to amend the Agreement as follows:\n"
                          "1. Article 3.01 is amended to read as follows:\nThe Borrower shall maintain the PMU.\n"
                          "2. The table in Schedule 2 is deleted and replaced with the following:\n"
                          "Amount of the Loan Allocated (expressed in USD)\n"
                          "(1) Goods and operating expenses\n34,912,500 100%\n(2) Emergency expenses\n0"},
        {"no": 2, "text": "(3) Front-end Fee 87,500\nTOTAL AMOUNT 35,000,000\n"
                          "3. A withdrawal restriction is added.\nAll other provisions remain unchanged.\n"
                          "This becomes effective as of the date of the countersignature.\nDate: ______"},
    ]
    output = run_job(fake_job("EXTRACT_AMENDMENT"), {"pages": pages})
    check("result", output)
    assert output["effectiveDate"] is None and output["amendedSet"] == []
    terms = [f for f in output["fields"] if f["group"] == "AGREEMENT_TERM"]
    provisions = [f for f in terms if f["value"]["key"] == "AMENDMENT_PROVISION"]
    assert [f["value"]["amendmentItem"] for f in provisions] == ["1", "2", "3"]
    allocations = [(f["value"]["amount"], f["source"]["page"]) for f in terms if "amount" in f["value"]]
    assert allocations == [(34_912_500, 1), (0, 1), (87_500, 2), (35_000_000, 2)]
    assert all(f["status"] == "NEEDS_REVIEW" for f in terms)


def test_a_limited_waiver_keeps_all_clause_targets_and_earlier_termination():
    pages = [
        {"no": 1, "text": "The lenders hereby waive the Company's (i) breach of Section 5.01(a) of the Agreement, "
                          "(ii) breach of Section 6.09 for the period ending on or about December 31, 2008, "
                          "(iii) breach of Section 6.10 for the period ending March 31, 2009, and"},
        {"no": 2, "text": "(iv) breach of Section 6.12 for the period ending December 31, 2008, provided such "
                          "waivers shall expire on a Waiver Termination Event, and upon such expiration the sections "
                          "shall be effective as if such waivers had not been given. Waiver Termination Event means "
                          "the earliest to occur of (A) 5 p.m. on October 13, 2009 or (B) capex exceeds $4,000,000. "
                          "2. Representations and Warranties."},
    ]
    output = run_job(fake_job("EXTRACT_WAIVER"), {"pages": pages})
    check("result", output)
    waivers = [f for f in output["fields"] if f["group"] == "WAIVER"]
    assert [f["value"]["target"]["clause"] for f in waivers] == ["5.01(a)", "6.09", "6.10", "6.12"]
    assert [f["source"]["page"] for f in waivers] == [1, 1, 1, 2]
    assert [f["value"]["periods"][0]["testDate"] for f in waivers[1:]] == ["2008-12-31", "2009-03-31", "2008-12-31"]
    assert all(f["value"]["expiresOn"] == "2009-10-13" for f in waivers)
    assert all(any("$4,000,000" in c for c in f["value"]["conditions"]) for f in waivers)
    assert all("as if such waivers" in f["value"]["reinstatement"] for f in waivers)
    assert all(f["value"]["target"]["appliedCovenantId"] is None for f in waivers)
