"""Phase 2 of "never miss silently": the AI covenant reader is grounded on the page and votes with the rules."""

from __future__ import annotations

import json

from dps.extraction.agreement import extract_terms
from dps.extraction.agreement_ai import covenant_pages, read
from dps.extraction.safety_net import possible_covenants
from dps.extraction.verify import number_on_line, quote_on_page, threshold_in
from dps.parse.engine import Page

COVENANTS = Page(7, "7.11 Financial Covenants.\n"
                    "(a) Consolidated Leverage Ratio. The Borrower will not permit the Consolidated Leverage Ratio as "
                    "at the last day of any Test Period to exceed 5.50 to 1.0.\n"
                    "(b) Consolidated Senior Secured Leverage Ratio. The Borrower will not permit the Consolidated "
                    "Senior Secured Leverage Ratio as at the last day of any Test Period to exceed 3.50 to 1.0.\n"
                    "(c) Debt Yield. The Borrower shall maintain a Debt Yield of not less than 9.0%.\n", "TEXT", 1.0)
OTHER = Page(2, "Definitions. Applicable Rate means 1.50% per annum.", "TEXT", 1.0)


def model(covenants, page=7):
    def completion(messages):
        assert f"=== Page {page} ===" in messages[0]["content"]
        return json.dumps({"covenants": covenants})
    return completion


def _rules():
    pages = [OTHER, COVENANTS]
    terms = extract_terms(pages)
    found, _ = possible_covenants(pages, terms)
    return pages, terms + found


def test_grounding_needs_the_quote_on_the_page_and_the_number_in_it():
    pages = {7: COVENANTS.text}
    quote = "The Borrower will not permit the Consolidated Leverage Ratio as at the last day"
    assert quote_on_page(pages, 8, quote) == 7  # the cited page or the one next to it
    assert quote_on_page(pages, 7, "the Leverage Ratio must stay under 5.5") is None  # not on the page
    assert quote_on_page(pages, 7, "Leverage Ratio") is None  # too short to prove anything
    assert threshold_in("to exceed 5.50 to 1.0", 5.5) and not threshold_in("to exceed 5.50 to 1.0", 4.5)
    assert number_on_line("Revenue from operations 25 16,363.10\nOther 9", 16363.1, "Revenue from operations")
    assert not number_on_line("Revenue from operations 25 16,363.10", 16363.1, "Other income")


def test_rules_and_model_agree_disagree_and_add():
    pages, terms = _rules()
    lev = [t for t in terms if t.metric == "DEBT_TO_EBITDA"]
    assert sorted(t.threshold for t in lev) == [3.5, 5.5]
    voted = read(pages, terms, model([
        # agrees with the rules: confirmed
        {"name": "Consolidated Leverage Ratio", "family": "LEVERAGE", "dir": "max", "threshold": 5.5, "page": 7,
         "quote": "permit the Consolidated Leverage Ratio as at the last day of any Test Period to exceed 5.50"},
        # same named covenant, another number (the model read a different level): questioned
        {"name": "Consolidated Senior Secured Leverage Ratio", "family": "LEVERAGE", "dir": "max", "threshold": 1.0,
         "page": 7, "quote": "Senior Secured Leverage Ratio as at the last day of any Test Period to exceed 3.50 to "
                             "1.0"},
        # made up: not on the page, dropped
        {"name": "Interest Coverage Ratio", "family": "ICR", "dir": "min", "threshold": 2.5, "page": 7,
         "quote": "The Interest Coverage Ratio shall not be less than 2.50 to 1.00 at any time."},
        # only the model knows this covenant: proposed for review, and the safety net's sentence goes away
        {"name": "Debt Yield", "family": "OTHER", "dir": "min", "threshold": 9.0, "page": 7,
         "quote": "The Borrower shall maintain a Debt Yield of not less than 9.0%."},
    ]))
    by = {(t.metric, t.threshold): t for t in voted if t.category == "FINANCIAL"}
    total = by[("DEBT_TO_EBITDA", 5.5)]
    assert (total.method, total.confidence) == ("REGEX+AI", 0.97)
    # The quote holds 3.50 (the rules' number), not 1.0: the model's answer fails grounding and changes nothing.
    senior = by[("DEBT_TO_EBITDA", 3.5)]
    assert senior.method == "REGEX" and not any(i.startswith("AI_READ_DIFFERENT") for i in senior.issues)
    assert senior.issues[-1] == "AI_DID_NOT_CONFIRM" and "AI_DID_NOT_CONFIRM" not in total.issues
    assert not any(t.metric == "ICR" for t in voted)
    debt_yield = [t for t in voted if t.category == "CANDIDATE"]
    assert len(debt_yield) == 1 and debt_yield[0].method == "AI" and debt_yield[0].name == "Debt Yield"
    assert debt_yield[0].issues == ["AI_ONLY: found by the AI reader, not by the rules"]


def test_a_grounded_different_number_is_questioned_and_a_new_covenant_proposed():
    page = Page(3, "6.1 Debt Service Coverage Ratio. The Borrower shall maintain a Debt Service Coverage Ratio of "
                   "not less than 1.25 times. From the second year the Debt Service Coverage Ratio shall be not less "
                   "than 1.40 times.\n6.2 The Fixed Charge Coverage Ratio shall be at least 1.10 to 1.00 at the end "
                   "of each quarter.", "TEXT", 1.0)
    terms = [t for t in extract_terms([page]) if t.category == "FINANCIAL"]
    voted = read([page], [t for t in terms if t.metric == "DSCR"], model([
        {"name": "Debt Service Coverage Ratio", "family": "DSCR", "dir": "min", "threshold": 1.4, "page": 3,
         "quote": "From the second year the Debt Service Coverage Ratio shall be not less than 1.40 times."},
        {"name": "Fixed Charge Coverage Ratio", "family": "FCCR", "dir": "min", "threshold": 1.1, "page": 3,
         "quote": "The Fixed Charge Coverage Ratio shall be at least 1.10 to 1.00 at the end of each quarter."},
    ], page=3))
    dscr = [t for t in voted if t.metric == "DSCR"]
    assert len(dscr) == 1 and dscr[0].threshold == 1.25
    assert dscr[0].issues == ["SEVERAL_LEVELS: the clause also gives 1.4", "AI_READ_DIFFERENT: >= 1.4 on page 3"]
    [fccr] = [t for t in voted if t.metric == "FIXED_CHARGE_COVERAGE_RATIO"]
    assert (fccr.method, fccr.operator, fccr.threshold, fccr.issues) == (
        "AI", ">=", 1.1, ["AI_ONLY: found by the AI reader, not by the rules"])


def test_model_outage_or_nothing_to_read_leaves_the_rules_alone():
    pages, terms = _rules()

    def down(messages):
        raise TimeoutError("model down")
    assert read(pages, terms, down) == terms
    assert covenant_pages([OTHER], []) == []


def test_flow_a_uses_the_ai_reader_when_configured(tmp_path):
    from test_flows import FakeCore, FakeParser, add_document, terms

    from dps.db import Database
    from dps.flows.intake import AgreementIntakeFlow, IntakeDeps
    from dps.models import Base

    db = Database(f"sqlite:///{tmp_path / 'ai.db'}")
    Base.metadata.create_all(db.engine)
    completion = model([{"name": "Consolidated Leverage Ratio", "family": "LEVERAGE", "dir": "max",
                         "threshold": 5.5, "page": 7,
                         "quote": "permit the Consolidated Leverage Ratio as at the last day of any Test Period to "
                                  "exceed 5.50"}])
    deps = IntakeDeps(db.session_factory, lambda d: b"pdf", FakeParser([OTHER, COVENANTS]), FakeCore([]), 0.8,
                      False, lambda pages, found: read(pages, found, completion))
    doc_id = add_document(db)
    AgreementIntakeFlow(deps).kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    stored = terms(db, doc_id)
    assert stored["DEBT_TO_EBITDA"].method == "REGEX+AI" and stored["DEBT_TO_EBITDA"].confidence == 0.97


def test_percent_coverage_is_times_and_forms_and_formulas_are_not_covenants():
    page = Page(7, "The Borrower shall maintain at all times a Cash Flow Coverage of not less than one hundred twenty "
                   "five percent (125%), calculated quarterly.\nAs of the Quarter-Annual Date, Guarantor's Leverage "
                   "Ratio is: [_______________]. Maximum Per Guaranty: 65%.\nBorrower shall maintain a Maximum Total "
                   "Liabilities to Total Tangible Assets of no greater than 70.00% at all times.", "TEXT", 1.0)
    voted = read([page], [], model([
        {"name": "Cash Flow Coverage", "family": "DSCR", "dir": "min", "threshold": 125, "page": 7,
         "quote": "The Borrower shall maintain at all times a Cash Flow Coverage of not less than one hundred twenty "
                  "five percent (125%)"},
        {"name": "Leverage Ratio", "family": "LEVERAGE", "dir": "max", "threshold": 65, "page": 7,
         "quote": "Guarantor's Leverage Ratio is: [_______________]. Maximum Per Guaranty: 65%."},
        {"name": "Total Liabilities to Total Tangible Assets", "family": "LEVERAGE", "dir": "max", "threshold": 70,
         "page": 7, "quote": "Borrower shall maintain a Maximum Total Liabilities to Total Tangible Assets of no "
                             "greater than 70.00% at all times."},
        {"name": "Cash Flow Coverage", "family": "DSCR", "dir": "min", "threshold": 125, "page": 7,
         "quote": "The Borrower shall maintain at all times a Cash Flow Coverage of not less than one hundred twenty "
                  "five percent (125%)"},
    ]))
    [dscr] = [t for t in voted if t.category == "FINANCIAL"]
    assert (dscr.metric, dscr.threshold) == ("DSCR", 1.25)  # 125% is 1.25 times; the repeat is not added again
    [other] = [t for t in voted if t.category == "CANDIDATE"]
    assert other.name == "Total Liabilities to Total Tangible Assets"  # a ratio DPS has no metric for
