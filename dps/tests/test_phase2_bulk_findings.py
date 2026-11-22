"""Phase 2 (22 Nov 2026): what a bulk run of 72 real public documents (US, UK, Australia, Singapore, UAE, India,
Sri Lanka) showed. Each case uses the wording of the document it came from."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from test_phase3_analyze import model_of

from dps.analyze.classify import classify
from dps.extraction.agreement import extract_terms
from dps.parse.engine import Page


def financial(text: str) -> list[tuple]:
    return [(t.metric, t.operator, t.threshold) for t in extract_terms([Page(1, text, "TEXT", 1.0)])
            if t.category == "FINANCIAL"]


# ---------- covenants that were read the wrong way or missed ----------
CSC = ("SECTION 22 FINANCIAL COVENANTS\n22.1 Minimum Interest Coverage Ratio\nThe Original "
       "Guarantor shall not permit at the end of any quarterly financial reporting period the ratio of Consolidated "
       "EBITDA to Consolidated Interest Expense for the period of four consecutive fiscal quarters ending on the last "
       "day of such quarterly financial reporting period, taken as a single period, to be less than 3.00 to 1.00.\n"
       "22.2 Consolidated Total Debt to Consolidated EBITDA Ratio\nThe Original Guarantor will not permit at the end "
       "of any quarterly financial reporting period the ratio of Consolidated Total Debt as of the last day of such "
       "quarterly financial reporting period to Consolidated EBITDA for the period of four consecutive fiscal "
       "quarters ending on the last day of such quarterly financial reporting period, taken as a single period, to "
       "exceed 3.00 to 1.00.\nSECTION 23 GENERAL UNDERTAKINGS\n")


def test_a_will_not_permit_far_before_the_number_is_still_a_floor_and_the_debt_ratio_is_found():
    """CSC Australia: "will not permit ... [250 characters] ... to be less than 3.00" was read as ICR < 3.0, and
    "Consolidated Total Debt to Consolidated EBITDA" was not found at all."""
    found = financial(CSC)
    assert ("ICR", ">=", 3.0) in found
    assert ("DEBT_TO_EBITDA", "<=", 3.0) in found


# ---------- numbers that are not covenants ----------
NOT_COVENANTS = [
    ("a margin grid", "4.3 Margin\nNet Debt to EBITDA Margin - Tranche1 Margin - Tranche2 Margin -Tranche3\n"
                      "> 3.00x 3.60% 3.75% 3.75%\n"),
    ("a penal-charges table", "SCHEDULE OF CHARGES\nMonthly basis Non submission of Net- worth Statement Absolute "
                              "INR 5,000 Monitoring on monthly basis\n"),
    ("a condition precedent", "Part 2 Conditions precedent\n(e) Facility A Repayment Schedule: evidence that the "
                              "Facility A Repayment Schedule, based on: (i) the Base Case Financial Model; (ii) a "
                              "minimum Debt Service Cover Ratio of 1.50 times; and\n"),
    ("a prepayment that moves a ratio", "7.4 Standstill\nthe Loans are prepaid by an amount such that the Back-Lever "
                                        "LTV (as at the first Standstill Commencement Date) is reduced by at least 20 "
                                        "per cent.\n"),
    ("a lien basket", "7.01 Liens\n(y) Liens securing Indebtedness or other obligations on the Collateral that are "
                      "pari passu with the Lien on the Collateral securing the Obligations, the First Lien Leverage "
                      "Ratio does not exceed 3.75:1.00\n"),
    ("a debt basket", "7.03 Indebtedness\nin the case of Indebtedness that is unsecured or secured by assets that are "
                      "not Collateral, the Total Leverage Ratio does not exceed 4.25:1.00\n"),
    ("the EBITDA of past quarters", "“Consolidated EBITDA” has the meaning given. Notwithstanding the "
                                    "foregoing, Consolidated EBITDA shall be $233,000,000, $257,000,000 and "
                                    "$234,000,000 for the fiscal quarters ended March 31, June 30 and September 30.\n"),
]


def test_numbers_that_are_not_covenants_are_not_read_as_covenants():
    """False findings of the bulk run: Foxtel's margin grid, DBS's penal charges, MAC's conditions precedent, a
    standstill prepayment, Travel + Leisure's lien and debt baskets and its EBITDA of past quarters."""
    for label, text in NOT_COVENANTS:
        assert financial(text) == [], (label, financial(text))


def test_a_real_covenant_next_to_the_new_checks_is_still_found():
    """The new checks look at the text around a number: a covenant clause of its own still reads."""
    text = ("7.11 Financial Covenants\n(a) Consolidated Net Leverage Ratio. Permit the Consolidated Net Leverage Ratio "
            "as of the end of any fiscal quarter of the Company to be greater than 3.75 to 1.00.\n")
    assert ("NET_LEVERAGE_RATIO", "<=", 3.75) in financial(text)


# ---------- document types ----------
def test_waivers_are_waivers():
    """NCI and Select Comfort ("WAIVER This Waiver ... (this "Waiver")") were UNKNOWN; Enhabit's "LIMITED WAIVER"
    was read as a loan agreement."""
    us = ("EX-10.1 WAIVER\nTHIS WAIVER, dated as of August 21, 2009 (this “Waiver”), to the Credit Agreement "
          "among NCI Building Systems, Inc. as Borrower and the Lenders. The Borrower has not complied with "
          "Section 5.9 "
          "and the Required Lenders hereby waive compliance with Section 5.9 for the Waiver Period.\n")
    assert classify(model_of(us)).type == "WAIVER_CONSENT"
    limited = ("Execution Version\nLIMITED WAIVER\nTHIS LIMITED WAIVER is entered into among the Borrower, the "
               "Lenders party hereto and Wells Fargo Bank as Administrative Agent under the Credit Agreement.\n")
    assert classify(model_of(limited)).type == "WAIVER_CONSENT"


def test_an_amendment_deed_and_a_lenders_amendment_letter_are_amendments():
    deed = ("EXECUTION VERSION\nAMENDMENT DEED No. 1\nin relation to the Syndicated Facility Agreement dated 2017\n"
            "Each party agrees that the Facility Agreement is amended as set out in this deed.\n")
    assert classify(model_of(deed)).type == "AMENDMENT"
    letter = ("Mr. S.R. Attygalle Secretary Ministry of Finance\nRe: Loan No. 9085-LK Amendment to the Loan "
              "Agreement\nWe refer to the Loan Agreement dated 2020. The Loan Agreement is hereby amended as follows: "
              "Section 2 shall be deleted and replaced with the following.\n")
    assert classify(model_of(letter)).type == "AMENDMENT"


def test_trust_deeds_are_trust_deeds_and_read_for_covenants():
    """Four debenture trust deeds (Sri Lanka, India) were UNKNOWN, so their covenants were never read."""
    india = ("DEBENTURE TRUST DEED\nDated 26 April 2021\nBETWEEN CITICORP FINANCE (INDIA) LIMITED Company AND IDBI "
             "TRUSTEESHIP SERVICES LIMITED Debenture Trustee for the Debenture Holders of the secured redeemable "
             "non-convertible debentures.\n")
    assert classify(model_of(india)).type == "TRUST_DEED"
    lanka = ("TRUST DEED\nFOR AN ISSUE OF TEN MILLION (10,000,000) DEBENTURES CONSTITUTED OF TYPE A LISTED RATED "
             "SENIOR "
             "UNSECURED REDEEMABLE DEBENTURES\nbetween First Capital Holdings PLC and Bank of Ceylon as Trustee\n")
    assert classify(model_of(lanka)).type == "TRUST_DEED"
    from dps.skills.runner import ONBOARD_PACKS

    assert ONBOARD_PACKS["TRUST_DEED"] == "covenants"


def test_master_facility_agreements_and_a_titled_loan_agreement():
    """Kotak's master facility agreement and Tata Capital's master terms were UNKNOWN; IDFC's business loan agreement,
    which mentions the sanction letter, was read as a sanction letter."""
    kotak = "MASTER FACILITY AGREEMENT\nI N D E X\nRecitals\nARTICLE 1 Definitions\nARTICLE 2 The Facility\n"
    assert classify(model_of(kotak)).type == "LOAN_AGREEMENT"
    tata = ("1\nMASTER TERMS AND CONDITIONS\n(Applicable for Term Loan)\nThese MASTER TERMS AND CONDITIONS (“T&Cs"
            "”) shall be applicable to term loan extended/to be extended by TATA CAPITAL LIMITED (hereinafter "
            "referred to as the “Lender”) to its borrowers under the Sanction Letter.\n")
    assert classify(model_of(tata)).type == "LOAN_AGREEMENT"
    idfc = ("BUSINESS INSTA LOAN AGREEMENT\nLOAN AGREEMENT NO.\nThis Loan Agreement is made and executed. "
            "“Sanction Letter” means the sanction letter issued by the Bank. The Borrower agrees to the terms "
            "and conditions of the sanction letter. The Bank has sanctioned the loan.\n")
    assert classify(model_of(idfc)).type == "LOAN_AGREEMENT"


def test_an_amendment_to_a_credit_agreement_stays_an_amendment():
    """The new title rule ("... CREDIT AGREEMENT" on page 1) must not turn an amendment or a restatement into the
    agreement itself."""
    amendment = ("EXHIBIT 10.1\nFOURTH AMENDMENT TO CREDIT AGREEMENT\nFOURTH AMENDMENT, dated as of 2023 (this "
                 "“Amendment”), to the Credit Agreement dated as of May 31, 2018 among the Borrower and the "
                 "Lenders.\n")
    assert classify(model_of(amendment)).type == "AMENDMENT"
    restated = ("EIGHTH AMENDED AND RESTATED CREDIT AGREEMENT\namong Core Laboratories as Borrower, the Lenders and "
                "Bank of America as Administrative Agent. This Agreement amends and restates the Seventh Amended and "
                "Restated Credit Agreement in its entirety.\n")
    assert classify(model_of(restated)).type == "AMENDED_AND_RESTATED"


# ---------- the bulk test tool ----------
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bulk_check.py"
spec = importlib.util.spec_from_file_location("bulk_check_phase2", SCRIPT)
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)


def test_the_manifest_is_not_read_as_a_document(tmp_path):
    for name in ("a.pdf", "b.xlsx", "manifest.csv", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in bulk.documents(tmp_path, tmp_path / "manifest.csv")] == ["a.pdf", "b.xlsx"]
    assert [p.name for p in bulk.documents(tmp_path, None)] == ["a.pdf", "b.xlsx"]


def test_certified_figures_and_agreement_terms_are_counted():
    result = {"fields": [{"group": "CERTIFIED_FIGURE", "value": {"covenant": "DSCR", "figure": 2.22}},
                         {"group": "AGREEMENT_TERM", "value": {"key": "SANCTIONED_AMOUNT"}}]}
    row = bulk.report_row(Path("c.pdf"), {"jobType": "EXTRACT_CERTIFICATE"}, {"status": "NEEDS_REVIEW"}, result, 1.0,
                          [])
    assert (row["certifiedFigures"], row["agreementTerms"], row["covenants"]) == (1, 1, 0)


# ---------- certificates ----------
HEREFORDSHIRE = ("Ratio Compliance Certificate\nTo: Worcestershire County Council and The County of Herefordshire "
                 "District Council (\"The Lenders\").\nPursuant to clause 15.8 (Ratio Compliance Certificate) of the "
                 "Senior Term Loan Facilities Agreement we confirm that: the Historic Annual Debt Service Cover Ratio "
                 "on 30 June 2024 was 2.22; (ii) the Projected Annual Debt Service Cover Ratio on 30 June 2024 was "
                 "1.98; and (iii) the Loan Life Cover Ratio on 30 June 2024 as 9.05 : 1.\nDirector\n")


def test_a_certificate_figure_read_by_the_rules_and_the_ai_is_one_finding():
    """Herefordshire: the rules' "DSCR 2.22" and the AI's "Historic Annual Debt Service Cover Ratio 2.22" were two
    findings; the projected DSCR (1.98) is another figure and stays its own finding."""
    from dps.skills import load
    from dps.skills.runner import _agent_only, _vote

    pack = load("certificate")
    pages = model_of(HEREFORDSHIRE)["pages"]
    fields = [f for f in pack.rules(pages, {}) if f["group"] == "CERTIFIED_FIGURE"]
    dscr = [f for f in fields if f["name"] == "DSCR"]
    assert dscr and dscr[0]["value"]["figure"] == 2.22
    proposals = [{"name": "Historic Annual Debt Service Cover Ratio", "page": 1,
                  "quote": "Historic Annual Debt Service Cover Ratio on 30 June 2024 was 2.22",
                  "value": {"figure": 2.22, "unit": "TIMES"}},
                 {"name": "Projected Annual Debt Service Cover Ratio", "page": 1, "quote": "Projected Annual Debt "
                  "Service Cover Ratio on 30 June 2024 was 1.98", "value": {"figure": 1.98, "unit": "TIMES"}}]
    fields = _vote(pack, fields, proposals)
    assert dscr[0]["method"] == "RULES+AI_CONFIRMED" and dscr[0]["confidence"] >= 0.95
    added = _agent_only(pack, pages, fields, proposals)
    assert [f["value"]["figure"] for f in added] == [1.98]


def test_the_uk_cover_ratios_have_their_taxonomy_codes():
    from dps.skills.covenants.taxonomy import get

    tax = get()
    assert tax.code_for("Loan Life Cover Ratio") == "FIN_LLCR"
    assert tax.code_for("Project Life Cover Ratio") == "FIN_PLCR"


def test_the_type_is_read_past_a_filing_cover_line_and_a_blank_page():
    """Sunseeker: page 1 is "EX-10.20 3 exhibit1020sunseekercred.htm EX-10.20", page 2 is blank, the title is on page
    3; read from the first two pages, the credit agreement was UNKNOWN and none of its covenants was read."""
    title = ("CREDIT AGREEMENT dated as of June 30, 2022 among SUNSEEKER as Borrower, the Lenders party hereto and the "
             "Administrative Agent. The Lenders agree to make loans to the Borrower. ") * 3
    terms = ("ARTICLE I DEFINITIONS. “Applicable Rate” means the rate per annum. The Borrower shall pay "
             "interest for each Interest Period. Event of Default means any of the events. ") * 3
    model = model_of("EX-10.20 3 exhibit1020sunseekercred.htm EX-10.20", "", title, terms)
    assert classify(model).type == "LOAN_AGREEMENT"
