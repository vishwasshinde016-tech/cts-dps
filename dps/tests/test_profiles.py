"""Country profiles (Phase 2, 11 November 2026): the borrower's profile (Core) decides the loan currency, the
financial-year end, group or company figures and the number style; the document's own numbers can overrule the
style, and a statement waits whenever something cannot be told."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import reviewed_if_clean
from statement_shim import StatementDeps, StatementProcessingFlow
from test_flows import FakeParser, add_document, doc_status, values, xlsx_bytes

from dps.db import Database
from dps.extraction.statement import detect_number_style, number_style, parse_number, rows_from_csv
from dps.models import Base
from dps.money import detect_currency
from dps.profiles import LoanProfile, currency_words

PROFILES = [
    {"code": "IN", "currency": "INR", "currencyWords": ["Rs.", "INR", "Indian Rupees"]},
    {"code": "LK", "currency": "LKR", "currencyWords": ["LKR", "Rs.", "SLRs"]},
    {"code": "KE", "currency": "KES", "currencyWords": ["KES", "KShs", "Kenya Shillings"]},
    {"code": "XX", "currency": "bad", "currencyWords": ["nothing"]},
]
LANKA = LoanProfile("LKR", True, 3, "CONSOLIDATED", "WESTERN", "LK")


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'profiles.db'}")
    Base.metadata.create_all(db.engine)
    return db


def run(database, data: bytes, name: str, profiles: dict, cps=("CP-1",), rate=None):
    doc_id = add_document(database, "FINANCIAL_STATEMENT", name, date(2026, 3, 31), cps)
    deps = StatementDeps(database.session_factory, lambda d: data, FakeParser([]), "ds-dps", True,
                         loan_profile=profiles.get, currency_words=lambda: currency_words(PROFILES),
                         fx_rate=lambda s, d, on: rate)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    return reviewed_if_clean(flow), doc_id, deps


# ---------------------------------------------------------------- profiles

def test_a_profile_from_core():
    p = LoanProfile.from_core({"currency": "LKR", "currencyKnown": True, "fiscalYearEndMonth": 12,
                               "statementEntity": "CONSOLIDATED", "numberStyle": "EUROPEAN", "country": "LK",
                               "currencyWords": ["LKR", "", 5]})
    assert p == LoanProfile("LKR", True, 12, "CONSOLIDATED", "EUROPEAN", "LK", ("LKR",))
    odd = LoanProfile.from_core({"currency": "LKR", "currencyKnown": False, "fiscalYearEndMonth": 13,
                                 "statementEntity": "GROUP", "numberStyle": "ROMAN"})
    assert (odd.currency_known, odd.fiscal_year_end_month, odd.statement_entity, odd.number_style) == (
        False, 3, "STANDALONE", "WESTERN")
    assert LoanProfile.from_core({"currency": "rupees"}) is None


def test_a_country_added_in_core_is_recognised_and_shared_words_name_no_currency():
    words = currency_words(PROFILES)
    assert detect_currency("Amounts in KShs million", words) == "KES"
    assert detect_currency("Kenya Shillings '000", words) == "KES"
    assert detect_currency("Amounts in KShs million") is None  # not a built-in currency word
    assert detect_currency("Rs. '000", words) is None  # India, Sri Lanka: the loan's currency is used
    assert detect_currency("SLRs million", words) == "LKR"


# ---------------------------------------------------------------- number style

@pytest.mark.parametrize("text, style", [
    ("Umsatz 1.234.567,89 Ergebnis 200.000,00", "EUROPEAN"),
    ("Revenue 1,234,567.89", "WESTERN"),
    ("Total 1,50,000", "WESTERN"),
    ("2025 2024 500", None),
    ("A 1.234.567,89 B 1,234,567.89", "UNCLEAR"),
    ("A 1.234.567,89 " * 10 + "B 1,234,567.89", "EUROPEAN"),
])
def test_detect_number_style(text, style):
    assert detect_number_style(text) == style


def test_european_numbers():
    assert parse_number("1.234.567,89") is None  # not a number in the comma style
    with number_style("EUROPEAN"):
        assert parse_number("1.234.567,89") == 1234567.89
        assert parse_number("(12.500)") == -12500
        assert parse_number("-3,5") == -3.5
    assert parse_number("1,234,567.89") == 1234567.89  # the style ends with the block


def test_a_european_csv_is_read_with_semicolons_and_comma_decimals():
    data = b"Posten;2026;2025\nEBITDA;1.234,5;1.100,0\nNet worth;9.000,0;8.000,0\n"
    with number_style("EUROPEAN"):
        rows = rows_from_csv(data)
    assert [(r[0], r[1]) for r in rows] == [("EBITDA", 1234.5), ("Net worth", 9000.0)]


EURO_CSV = (b"Amounts in EUR million\nLine item;2026;2025\nEBITDA;1.234,5;1.100,0\nTotal debt;2.500,0;2.400,0\n"
            b"Net worth;9.000,0;8.000,0\n")


def test_flow_b_reads_a_european_statement_by_its_own_numbers(database):
    """The borrower's profile says the comma style; the document's numbers say otherwise and win."""
    euro_loan = {"CP-1": LoanProfile("EUR", True, 12, "STANDALONE", "WESTERN", "DE")}
    flow, _, _ = run(database, EURO_CSV, "fs.csv", euro_loan)
    assert flow.state.number_style == "EUROPEAN"
    assert flow.state.outcome == "REPORTED", flow.state.issues
    v = values(database)
    assert (v["EBITDA"].value, v["EBITDA"].unit) == (1234.5, "EUR_MN")
    assert v["TOTAL_DEBT"].value == 2500.0


def test_flow_b_holds_a_statement_whose_number_style_cannot_be_told(database):
    mixed = (b"Amounts in EUR million\nLine item;2026\nEBITDA;1.234,5\nTotal debt;2,500.00\nNet worth;9.000,00\n"
             b"Cash;1,000.00\n")
    flow, _, _ = run(database, mixed, "fs.csv", {"CP-1": LoanProfile("EUR", True, 12, "STANDALONE", "EUROPEAN")})
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert any(i.startswith("NUMBER_STYLE_UNCLEAR") for i in flow.state.issues)


# ---------------------------------------------------------------- currency, year end, entity

def test_flow_b_waits_when_nobody_knows_the_loans_currency_then_reads_it_again(database):
    rows = [("All amounts in LKR million", None), ("EBITDA", 800), ("Short-term borrowings", 1200),
            ("Long-term borrowings", 1800), ("Total equity", 5000)]
    unknown = {"CP-1": LoanProfile("INR", False)}
    flow, doc_id, deps = run(database, xlsx_bytes(rows), "fs.xlsx", unknown)
    assert flow.state.outcome == "NEEDS_REVIEW" and doc_status(database, doc_id) == "NEEDS_REVIEW"
    assert flow.state.issues[0].startswith("LOAN_CURRENCY_UNKNOWN: the CP service did not say the currency of CP-1")
    assert values(database) == {}

    deps.loan_profile = {"CP-1": LANKA}.get  # the RM set the borrower's country; "Review done" reads it again
    again = StatementProcessingFlow(deps)
    again.kickoff(inputs={"id": flow.state.id, "job_id": "j1", "document_id": doc_id})
    assert again.state.outcome == "REPORTED"
    assert (values(database)["EBITDA"].value, values(database)["EBITDA"].unit) == (800.0, "LKR_MN")


def test_flow_b_reads_the_group_column_for_a_borrower_on_group_figures(database):
    rows = [("All amounts in LKR million", None, None), ("Particulars", "Company 2026", "Group 2026"),
            ("EBITDA", 500, 800), ("Short-term borrowings", 1000, 1200), ("Long-term borrowings", 1500, 1800),
            ("Total equity", 4000, 5000)]
    flow, _, _ = run(database, xlsx_bytes(rows), "fs.xlsx", {"CP-1": LANKA})
    assert flow.state.entity == "CONSOLIDATED"
    assert values(database)["EBITDA"].value == 800.0
    company = {"CP-1": LoanProfile("LKR", True, 3, "STANDALONE", "WESTERN")}
    flow2, _, _ = run(database, xlsx_bytes(rows), "fs2.xlsx", company, cps=("CP-2",))
    assert flow2.state.entity == "" or flow2.state.entity == "STANDALONE"


def test_flow_b_keeps_the_borrowers_year_end_for_twelve_months(database):
    december = {"CP-1": LoanProfile("LKR", True, 12, "STANDALONE", "WESTERN")}
    rows = [("All amounts in LKR million", None), ("EBITDA", 800), ("Short-term borrowings", 1200),
            ("Long-term borrowings", 1800), ("Total equity", 5000)]
    flow, doc_id, _ = run(database, xlsx_bytes(rows), "fs.xlsx", december)
    assert flow.state.fiscal_month == 12
    import json

    from dps.models import Document

    with database.session_factory() as s:
        review = json.loads(s.get(Document, doc_id).review_json)
    assert (review["fiscalYearEndMonth"], review["entity"], review["numberStyle"]) == (12, "STANDALONE", "WESTERN")


def test_flow_b_holds_a_statement_linked_to_borrowers_that_differ(database):
    rows = [("All amounts in LKR million", None), ("EBITDA", 800), ("Short-term borrowings", 1200),
            ("Long-term borrowings", 1800), ("Total equity", 5000)]
    two = {"CP-1": LANKA, "CP-2": LoanProfile("LKR", True, 12, "CONSOLIDATED", "WESTERN")}
    flow, _, _ = run(database, xlsx_bytes(rows), "fs.xlsx", two, cps=("CP-1", "CP-2"))
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert any(i.startswith("LOANS_DIFFER") for i in flow.state.issues)


def test_flow_b_converts_a_statement_in_a_currency_added_in_core(database):
    rows = [("Amounts in KShs million", None), ("EBITDA", 1000), ("Short-term borrowings", 2000),
            ("Long-term borrowings", 3000), ("Total equity", 9000)]
    usd_loan = {"CP-1": LoanProfile("USD", True, 12, "STANDALONE", "WESTERN")}
    flow, _, _ = run(database, xlsx_bytes(rows), "fs.xlsx", usd_loan, rate=0.0077)
    assert (flow.state.currency, flow.state.loan_currency) == ("KES", "USD")
    assert values(database)["EBITDA"].value == pytest.approx(7.7)  # KES 1,000 Mn at 0.0077


