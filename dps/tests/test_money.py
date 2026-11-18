"""Money for any country (10 November 2026): figures keep the loan's currency, INR in crore and any other in millions,
and a statement in another currency is converted at the bank's FX rate or waits for one."""

from __future__ import annotations

from datetime import date

import pytest
from test_bug_hunt import terms_of
from test_flows import FakeParser, add_document, doc_status, values, xlsx_bytes

from dps.db import Database
from dps.extraction.mapping import normalize
from dps.extraction.statement import extract_values
from dps.flows.statement import StatementDeps, StatementProcessingFlow
from dps.models import Base
from dps.money import currency_of, describe, detect_currency, money_unit, to_money_unit


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'money.db'}")
    Base.metadata.create_all(db.engine)
    return db

LKR_ROWS = [("All amounts in LKR million", None), ("EBITDA", 800), ("Short-term borrowings", 1200),
            ("Long-term borrowings", 1800), ("Total equity", 5000), ("Cash and cash equivalents", 300)]
USD_ROWS = [("Amounts in USD thousands", None), ("EBITDA", 2000), ("Short-term borrowings", 3000),
            ("Long-term borrowings", 1000), ("Total equity", 9000), ("Cash and cash equivalents", 500)]


@pytest.mark.parametrize("text, code", [
    ("All amounts in LKR million", "LKR"), ("Sri Lankan Rupees '000", "LKR"), ("₹ in crore", "INR"),
    ("Amounts in USD thousands", "USD"), ("Rs. '000", None), ("", None),
])
def test_detect_currency(text, code):
    assert detect_currency(text) == code


def test_money_units():
    assert (money_unit("INR"), money_unit("LKR")) == ("INR_CR", "LKR_MN")
    assert (currency_of("LKR_MN"), currency_of("INR_CR"), currency_of("x"), currency_of(None)) == ("LKR", "INR",
                                                                                                    None, None)
    assert to_money_unit(5000, "thousand", "LKR") == 5.0
    assert to_money_unit(500, "lakh", "INR") == 5.0
    assert to_money_unit(2, "million", "LKR", rate=300.0) == 600.0  # USD 2 Mn at 300 LKR a dollar
    assert describe(800, "LKR_MN").startswith("LKR 800")


def test_an_lkr_statement_is_kept_in_lkr_million():
    result = extract_values([(r[0], r[1], 1, None) for r in LKR_ROWS[1:]], LKR_ROWS[0][0], "LKR")
    ebitda = next(v for v in result.values if v.key == "EBITDA")
    assert (ebitda.value, ebitda.unit, result.money) == (800.0, "LKR_MN", "LKR_MN")


def run(database, rows, loan="LKR", rate=None, cps=("CP-LK",)):
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fs.xlsx", date(2026, 3, 31), cps)
    asked = []

    def fx(src, dst, on):
        asked.append((src, dst, on))
        return rate

    currencies = loan if isinstance(loan, dict) else dict.fromkeys(cps, loan)
    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(rows), FakeParser([]), "ds-dps", True,
                         loan_currency=currencies.get, fx_rate=fx)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    return flow, doc_id, asked, deps


def test_flow_b_reports_an_lkr_loan_in_lkr(database):
    flow, _, asked, _ = run(database, LKR_ROWS)
    assert flow.state.outcome == "REPORTED" and asked == []
    v = values(database, "CP-LK")
    assert (v["EBITDA"].value, v["EBITDA"].unit, v["EBITDA"].currency) == (800.0, "LKR_MN", "LKR")
    assert (v["TOTAL_DEBT"].value, v["TOTAL_DEBT"].unit) == (3000.0, "LKR_MN")


def test_flow_b_converts_a_usd_statement_at_the_banks_rate(database):
    flow, _, asked, _ = run(database, USD_ROWS, rate=300.0)
    assert flow.state.outcome == "REPORTED"
    assert asked == [("USD", "LKR", date(2026, 3, 31))]
    v = values(database, "CP-LK")
    assert (v["EBITDA"].value, v["EBITDA"].unit) == (600.0, "LKR_MN")  # USD 2 Mn x 300
    assert flow.state.fx_rate == 300.0 and flow.state.currency == "USD"


def test_flow_b_holds_a_statement_with_no_rate_and_rereads_it_after_review(database):
    flow, doc_id, _, deps = run(database, USD_ROWS, rate=None)
    assert flow.state.outcome == "NEEDS_REVIEW" and doc_status(database, doc_id) == "NEEDS_REVIEW"
    assert flow.state.issues[0].startswith("CURRENCY_DIFFERS: the statement is in USD, the loan in LKR")
    assert values(database, "CP-LK") == {}

    deps.fx_rate = lambda s, d, on: 300.0  # the rate was added in Settings; "Review done" reads it again
    again = StatementProcessingFlow(deps)
    again.kickoff(inputs={"id": flow.state.id, "job_id": "j1", "document_id": doc_id})
    assert again.state.outcome == "REPORTED" and values(database, "CP-LK")["EBITDA"].value == 600.0


def test_flow_b_review_done_without_the_rate_holds_again(database):
    flow, doc_id, _, deps = run(database, USD_ROWS, rate=None)
    again = StatementProcessingFlow(deps)
    again.kickoff(inputs={"id": flow.state.id, "job_id": "j1", "document_id": doc_id})
    assert again.state.outcome == "NEEDS_REVIEW" and values(database, "CP-LK") == {}


def test_flow_b_holds_a_statement_linked_to_loans_in_two_currencies(database):
    flow, _, _, _ = run(database, LKR_ROWS, loan={"CP-LK": "LKR", "CP-IN": "INR"}, cps=("CP-LK", "CP-IN"))
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert any(i.startswith("CURRENCY_MIXED") for i in flow.state.issues)


def test_flow_b_without_core_keeps_inr_crore(database):
    rows = [("Amounts in INR lakhs", None), ("EBITDA", 500), ("Short-term borrowings", 300),
            ("Long-term borrowings", 700), ("Total equity", 2000), ("Cash and cash equivalents", 100)]
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fs.xlsx", date(2026, 3, 31), ("CP-1",))
    deps = StatementDeps(database.session_factory, lambda d: xlsx_bytes(rows), FakeParser([]), "ds-dps", True)
    StatementProcessingFlow(deps).kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    v = values(database)["EBITDA"]
    assert (v.value, v.unit, v.currency) == (5.0, "INR_CR", "INR")


@pytest.mark.parametrize("amount, threshold", [
    ("LKR 1,000,000,000", 1000.0), ("Sri Lankan Rupees 2,500 million", 2500.0), ("Rs. 3 billion", 3000.0),
])
def test_an_lkr_loan_keeps_lkr_limits_in_lkr_million(amount, threshold):
    [p] = normalize(terms_of(f"7.1 Net Worth. The Borrower shall maintain a Tangible Net Worth of not less than "
                             f"{amount}."), [], "LKR")
    assert (p.unit, p.threshold) == ("LKR_MN", threshold)
    assert not any(i.startswith("AMOUNT_CURRENCY_DIFFERS") for i in p.issues)


def test_an_lkr_loan_holds_a_usd_limit():
    [p] = normalize(terms_of("7.1 Net Worth. The Borrower shall maintain a Tangible Net Worth of not less than "
                             "USD 50,000,000."), [], "LKR")
    assert p.unit == "USD" and p.expression is None
    assert any(i.startswith("AMOUNT_CURRENCY_DIFFERS: the limit is in USD, the loan in LKR") for i in p.issues)
