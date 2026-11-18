"""The covenants skill's agent answers get library keys, loan mechanics are dropped, one item per key
(live Azure run, 18 Nov 2026: the agent split clauses into made-up names and returned drawdown and cure terms)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_document_agent import Script, gateway
from test_phase3_analyze import model_of
from test_phase4_skills import Tools, fake_job

from dps.skills import load
from dps.skills import runner as skills
from dps.skills.covenants import names


@pytest.mark.parametrize("metric, name, key", [
    ("DSCR", "Debt Service Coverage Ratio", "DSCR"),
    ("TANGIBLE_NET_WORTH", None, "MIN_TANGIBLE_NET_WORTH"),
    ("DEBT_TO_NET_WORTH_RATIO", None, "DEBT_TO_NET_WORTH"),  # the longest alias wins, not "NET WORTH"
    ("Total Leverage Ratio", None, "DEBT_TO_EBITDA"),
    ("ANNUAL_FINANCIAL_STATEMENTS", None, "REPORTING_COVENANT"),
    ("MANAGEMENT_ACCOUNTS_OR_TAX_FILINGS", None, "REPORTING_COVENANT"),
    ("BREACH_OR_MATERIAL_ADVERSE_CHANGE_NOTICE", None, "REPORTING_COVENANT"),
    ("TAX_INSURANCE_AND_END_USE_EVIDENCE", None, "REPORTING_COVENANT"),
    ("RESTRICTION_ON_ADDITIONAL_BORROWING_OR_GUARANTEE", None, "DEBT_INCURRANCE_RESTRICTION"),
    ("RESTRICTION_ON_CHARGED_ASSET_DISPOSAL_LEASE_OR_ENCUMBRANCE", None, "ASSET_DISPOSAL_RESTRICTION"),
    ("RESTRICTION_ON_TRANSFER_OF_CONTROL", None, "CHANGE_OF_CONTROL"),
    ("RESTRICTION_ON_OBSTRUCTION_OF_INSPECTION", None, "BOOKS_AND_RECORDS"),
    ("LIEN_RESTRICTION", None, "NEGATIVE_PLEDGE"),
    ("RESTRICTION_ON_CHANGE_OF_BUSINESS", None, "OTHER"),
    (None, "Fixed Asset Coverage", "OTHER"),
])
def test_agent_names_map_to_library_keys(metric, name, key):
    assert names.canonical(metric, name) == key


@pytest.mark.parametrize("metric", ["DRAWDOWN_NOTICE", "MINIMUM_DRAWDOWN_AMOUNT", "PREPAYMENT_NOTICE",
                                    "PAYMENT_OBLIGATION", "FINANCIAL_COVENANT_CURE", "PAYMENT_DEFAULT",
                                    "EVENT_OF_DEFAULT", "UPFRONT_FEE", "INTEREST_PAYMENT"])
def test_loan_mechanics_are_dropped(metric):
    assert names.clean([{"name": metric, "page": 1, "quote": "q", "value": {"metric": metric}}]) == []


def test_a_covenant_key_is_never_dropped_as_a_mechanic():
    # ICR has "INTEREST" in its words; EBITDA_TO_EMI has "EMI"; neither is a loan mechanic.
    kept = names.clean([{"name": "Interest Coverage Ratio", "value": {"metric": "ICR"}},
                        {"name": "EBITDA to EMI", "value": {"metric": "EBITDA_TO_EMI"}}])
    assert [p["value"]["metric"] for p in kept] == ["ICR", "EBITDA_TO_EMI"]


def test_one_item_per_non_financial_key_and_other_keeps_the_written_name():
    out = names.clean([
        {"name": "Annual financial statements", "value": {"metric": "ANNUAL_FINANCIAL_STATEMENTS"}},
        {"name": "Management accounts", "value": {"metric": "MANAGEMENT_ACCOUNTS"}},
        {"name": "No change of business", "value": {"metric": "RESTRICTION_ON_CHANGE_OF_BUSINESS"}},
        {"name": "DSCR", "value": {"metric": "DSCR", "threshold": 1.2}},
        {"name": "DSCR step-down", "value": {"metric": "DSCR", "threshold": 1.1}}])
    assert [p["value"]["metric"] for p in out] == ["REPORTING_COVENANT", "OTHER", "DSCR", "DSCR"]
    assert out[1]["value"]["nameAsWritten"] == "No change of business" and out[0]["_written"] == \
        "Annual financial statements"


def test_amendments_keep_every_item_and_their_mechanics():
    items = [{"name": "Repayment schedule", "value": {"metric": "REPAYMENT_SCHEDULE"}},
             {"name": "a", "value": {"metric": "REPORTING_COVENANT"}},
             {"name": "b", "value": {"metric": "REPORTING_COVENANT"}}]
    assert len(names.clean(items, drop_mechanics=False, collapse=False)) == 3
    assert names.clean(None) is None


def test_the_prompt_lists_exactly_the_library_keys():
    prompt = (Path(names.__file__).parent / "prompt.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", prompt.split("library keys:")[1].split("Use OTHER")[0]))
    assert listed == set(names.KNOWN)


def test_agreement_job_reviewers_see_covenants_only_with_library_names():
    page = ("FINANCIAL COVENANTS\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 "
            "times, tested annually.\nThe Borrower may prepay the facility after giving not less than 30 days notice.\n"
            "The Borrower shall not create any lien over its assets without prior written consent of the Lender.\n"
            "The Borrower shall not change the general nature of its business.")
    items = {"items": [
        {"name": "Debt Service Coverage Ratio", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.20",
         "value": {"metric": "DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES"}},
        {"name": "Prepayment notice", "page": 1, "quote": "may prepay the facility after giving not less than 30 days",
         "value": {"metric": "PREPAYMENT_NOTICE"}},
        {"name": "Restriction on liens", "page": 1, "quote": "shall not create any lien over its assets",
         "value": {"metric": "RESTRICTION_ON_LIENS_AND_ENCUMBRANCES"}},
        {"name": "No change of business", "page": 1, "quote": "shall not change the general nature of its business",
         "value": {"metric": "RESTRICTION_ON_CHANGE_OF_BUSINESS"}}]}
    script = Script({("strong", "covenants"): items,
                     ("strong", "schedule_terms"): {"items": []}})
    result = skills.run(load("covenants"), fake_job("ONBOARD"), model_of(page), gateway(script), Tools())
    covenants = [f for f in result["fields"] if f["group"] != "SCHEDULE_TERM"]
    metrics = sorted(str(f["value"].get("metric")).upper() for f in covenants)
    assert "PREPAYMENT_NOTICE" not in metrics and not any("RESTRICTION_ON" in m for m in metrics)
    assert "DSCR" in metrics and "NEGATIVE_PLEDGE" in metrics and "OTHER" in metrics
    other = next(f for f in covenants if f["value"].get("metric") == "OTHER")
    assert other["status"] == "NEEDS_REVIEW" and other["value"]["nameAsWritten"] == "No change of business"


@pytest.mark.parametrize("metric, key", [("SECURITY_COVER", "SECURITY_COVER"), ("Asset Cover", "SECURITY_COVER"),
                                         ("Value of the Collateral", "MIN_COLLATERAL_VALUE"),
                                         ("ASSET_DISPOSAL_CAP", "ASSET_DISPOSAL_CAP")])
def test_security_covenants_the_rules_know_are_library_keys(metric, key):
    assert names.canonical(metric) == key and key in names.FINANCIAL
