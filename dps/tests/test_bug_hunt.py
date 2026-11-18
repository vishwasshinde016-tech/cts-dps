"""Bug hunt (8 November 2026, second round): each test was a wrong or lost figure or covenant before its fix."""

from __future__ import annotations

import pytest
from test_statement_pdf import amounts, header, line

from dps.extraction.agreement import _threshold, extract_terms
from dps.extraction.coverage import looks_like_covenant
from dps.extraction.mapping import foreign_currency, normalize
from dps.extraction.statement import extract_values, match_key, parse_number, rows_from_csv
from dps.extraction.statement_pdf import rows_from_words
from dps.parsing import Page


def values(rows, header_text="Amounts in INR crore"):
    return {v.key: v.value for v in extract_values(rows, header_text).values}


# ---------------------------------------------------------------- statements: this year's column only

def test_a_dash_in_this_years_column_is_nil_not_last_years_figure_csv():
    rows = rows_from_csv(b"Rs in crore,2026,2025\nEBITDA,500,400\nShort-term borrowings,-,100\n"
                         b"Long-term borrowings,700,600\nTotal equity,2000,1800\n")
    got = values(rows)
    assert got["ST_DEBT"] == 0.0 and got["TOTAL_DEBT"] == 700.0  # was 100, last year's, with no flag


def test_a_blank_in_this_years_column_never_takes_last_years_figure_csv():
    rows = rows_from_csv(b"Rs in crore,2026,2025\nEBITDA,500,400\nCurrent investments,,250\nTotal equity,2000,1800\n")
    assert "INVESTMENTS" not in values(rows)


def test_a_dash_or_blank_in_this_years_column_pdf():
    page = (line(10, ("Particulars", 40)) + header(10, ("Note", 310), ("31 March 2026", 440), ("31 March 2025", 540))
            + amounts(30, "Total equity", ("12", 310), ("4,055.36", 440), ("4,167.58", 540))
            + amounts(50, "Short-term borrowings", ("15", 310), ("-", 440), ("300.00", 540))
            + amounts(70, "Long-term borrowings", ("14", 310), ("2,342.93", 440), ("1,319.77", 540))
            + amounts(90, "Current investments", ("16", 310), ("500.00", 540)))
    got = values(rows_from_words([page]))
    assert got["ST_DEBT"] == 0.0 and got["TOTAL_DEBT"] == 2342.93 and "INVESTMENTS" not in got


@pytest.mark.parametrize("raw, expected", [
    ("−50", -50.0), ("− 1,234", -1234.0), ("$1,234", 1234.0), ("₹ 1,234", 1234.0),
    ("Rs. 1,234.5", 1234.5), ("1 234 567", 1234567.0), ("(1,234)", -1234.0), ("-", None), ("–", None),
])
def test_numbers_in_spreadsheet_cells(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("label", ["Income tax payable", "Deferred income tax asset", "Deferred income tax liability",
                                   "Income tax recoverable", "Income tax refund", "Advance income tax"])
def test_balance_sheet_tax_lines_are_not_the_tax_charge(label):
    # Held-out set: "Income tax payable | - |" (nil) and "Deferred income tax asset 272.2" were read as the tax charge.
    assert match_key(label) is None


@pytest.mark.parametrize("label", ["Income tax expense", "Provision for income taxes", "Income tax provision",
                                   "Total tax expense"])
def test_the_tax_charge_is_still_read(label):
    assert match_key(label) == "TAX"


# ---------------------------------------------------------------- agreements

def terms_of(text):
    return extract_terms([Page(1, "ARTICLE VII FINANCIAL COVENANTS\n" + text + "\n", "TEXT", 1.0)])


@pytest.mark.parametrize("sentence, op, value", [
    ("The Borrower shall ensure that the Interest Coverage Ratio is at all times greater than or equal to 2.5x.",
     ">=", 2.5),
    ("The Interest Coverage Ratio shall at all times be greater than or equal to 2.50:1.", ">=", 2.5),
    ("The Leverage Ratio shall at all times be less than or equal to 3.5x.", "<=", 3.5),
    ("The DSCR shall not at any time be lower than 1.2x.", ">=", 1.2),
    ("The Borrower shall keep its Current Ratio at or above 1.1.", ">=", 1.1),
    ("The Borrower shall not allow the Leverage Ratio to go beyond 3.5x.", "<=", 3.5),
    ("The Borrower undertakes that the DSCR will remain above 1.2x.", ">", 1.2),
    ("The Borrower shall not permit the Leverage Ratio to be greater than or equal to 3.5x.", "<", 3.5),
])
def test_wordings_the_rules_and_the_safety_net_missed(sentence, op, value):
    assert looks_like_covenant(sentence)
    assert _threshold(sentence, False, "ICR")[:2] == (op, value)


def test_a_net_worth_heading_over_a_tangible_net_worth_clause_is_one_covenant():
    terms = terms_of("7.1 Net Worth. The Borrower shall maintain a Tangible Net Worth of not less than INR 500 crore.")
    assert [(t.metric, t.threshold) for t in terms] == [("MIN_TANGIBLE_NET_WORTH", 500.0)]


@pytest.mark.parametrize("amount, currency", [
    ("LKR 1,000,000,000", "LKR"), ("$50,000,000", "USD"), ("USD 50,000,000", "USD"),
])
def test_an_amount_in_another_currency_is_held_not_read_as_crore(amount, currency):
    terms = terms_of(f"7.1 Net Worth. The Borrower shall maintain a Tangible Net Worth of not less than {amount}.")
    [p] = normalize(terms, [])
    assert p.unit == currency and p.expression is None and p.assessment_type == "MANUAL"
    assert any(i.startswith("AMOUNT_CURRENCY_DIFFERS") for i in p.issues)


def test_rupee_amounts_are_converted_to_crore():
    [p] = normalize(terms_of("7.1 Net Worth. The Borrower shall maintain a Tangible Net Worth of not less than "
                             "Rs. 500 million."), [])
    assert (p.unit, p.threshold, p.issues) == ("INR_CR", 50.0, [])


@pytest.mark.parametrize("quote, code", [
    ("not less than €50,000,000", "EUR"), ("not less than £50,000,000", "GBP"),
    ("Sri Lankan Rupees 500,000,000", "LKR"), ("INR 500 crore", None), ("Rs. 50 lakhs", None),
])
def test_foreign_currency_in_a_quote(quote, code):
    assert foreign_currency(quote) == code


@pytest.mark.parametrize("label, flagged", [
    ("Interest expense, net", True),
    ("Net finance costs", True),
    ("Finance costs (net)", True),
    ("Finance costs, net of amounts capitalised", False),
    ("Finance costs", False),
    ("Interest expense", False),
])
def test_net_interest_is_flagged(label, flagged):
    """Held-out set (HU01): interest net of interest income is not the gross interest a covenant may use."""
    result = extract_values([(label, 19.0, 1, None), ("EBITDA", 120.0, 1, None)], "Rs. in crore")
    assert any(i.startswith("INTEREST_IS_NET") for i in result.issues) is flagged
    assert [v.value for v in result.values if v.key == "INTEREST_PAID"] == [19.0]


@pytest.mark.parametrize("text, expected", [
    ("12.1 Financial Covenants. The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25:1 "
     "and a Leverage Ratio of not more than 3.50:1.", {("DSCR", ">=", 1.25), ("DEBT_TO_EBITDA", "<=", 3.5)}),
    ("12.1 The Borrower shall maintain a Leverage Ratio of not more than 3.50:1 and a Debt Service Coverage Ratio "
     "of not less than 1.25:1.", {("DSCR", ">=", 1.25), ("DEBT_TO_EBITDA", "<=", 3.5)}),
    ("12.1 The Borrower shall ensure that the DSCR is at least 1.20x and the Total Debt to EBITDA does not exceed "
     "4.00x.", {("DSCR", ">=", 1.2), ("DEBT_TO_EBITDA", "<=", 4.0)}),
    ("12.1 The Borrower shall maintain (a) a DSCR of not less than 1.25 times; and (b) a Debt to Equity Ratio of "
     "not more than 2.00 times.", {("DSCR", ">=", 1.25), ("DEBT_TO_EQUITY", "<=", 2.0)}),
    ("12.1 The Borrower shall maintain a Current Ratio of at least 1.33:1, a TOL/TNW of not more than 3:1 and an "
     "Interest Coverage Ratio of not less than 2.0x.",
     {("CURRENT_RATIO", ">=", 1.33), ("DEBT_TO_NET_WORTH", "<=", 3.0), ("ICR", ">=", 2.0)}),
])
def test_several_ratios_in_one_clause_each_get_their_own_number(text, expected):
    """Bug hunt 3: a clause with two ratios gave one ratio the other's number and invented a third covenant."""
    terms = extract_terms([Page(1, text, "TEXT", 0.9)])
    assert {(t.metric, t.operator, t.threshold) for t in terms} == expected
    assert all(not t.issues for t in terms)  # the other ratio's number is not a second level


@pytest.mark.parametrize("text, expected", [
    ("Minimum DSCR: 1.15x; Maximum Debt/EBITDA: 5.0x.", {("DSCR", ">=", 1.15), ("DEBT_TO_EBITDA", "<=", 5.0)}),
    ("The Borrower shall ensure that the Total Debt / EBITDA does not exceed 4.5x.", {("DEBT_TO_EBITDA", "<=", 4.5)}),
    ("The Borrower will not permit the Total Leverage Ratio to exceed three and one-half to one.",
     {("DEBT_TO_EBITDA", "<=", 3.5)}),
    ("The Borrower shall not permit the Consolidated EBITDA to fall below Rs. 500 Crores at any time.",
     {("MIN_EBITDA", ">=", 500.0)}),
    ("The Leverage Ratio shall not exceed (a) 4.00:1.00 for any Test Period ending on or before December 31, 2026 "
     "and (b) 3.50:1.00 thereafter.", {("DEBT_TO_EBITDA", "<=", 4.0)}),
    # Held-out HA08: the list heading "Permit:" with the "shall not" far above it.
    ("Section 6.2 Financial Covenants. Permit:\n(a) Total Leverage Ratio to be greater than 0.75 to 1.00 as of the "
     "last day of any month.\n(b) Tangible Net Worth to be less than $1,000,000,000 as of the last day of any month.",
     {("DEBT_TO_EBITDA", "<=", 0.75), ("MIN_TANGIBLE_NET_WORTH", ">=", 1_000_000_000.0)}),
])
def test_bug_hunt_3_wordings_are_read(text, expected):
    """Bug hunt 3 (9 November 2026): term-sheet labels, a spaced slash, ratios in words after "to exceed", minimum
    EBITDA outside an "EBITDA" clause and a list marker before the first level were only shown or missed."""
    terms = [t for t in extract_terms([Page(3, "ARTICLE 7 FINANCIAL COVENANTS\n" + text, "TEXT", 1.0)])
             if t.category == "FINANCIAL"]
    assert {(t.metric, t.operator, t.threshold) for t in terms} == expected


@pytest.mark.parametrize("text", [
    "Notwithstanding the foregoing, Consolidated Adjusted EBITDA for the fiscal quarter ended September 30, 2024 "
    "will be deemed to be $91,400,000.",
    "Investments in an aggregate amount not exceeding the greater of $115,000,000 and 30% of LTM EBITDA.",
    "If Consolidated EBITDA for any period is less than $10,000,000, the Borrower may cure the shortfall.",
])
def test_ebitda_that_is_not_a_floor_is_not_read_as_one(text):
    """Held-out set: a deemed figure and a basket were proposed as minimum EBITDA before the reader was tightened."""
    terms = extract_terms([Page(3, "ARTICLE 7 FINANCIAL COVENANTS\n" + text, "TEXT", 1.0)])
    assert not [t for t in terms if t.metric == "MIN_EBITDA"]


def test_a_short_label_line_reaches_the_safety_net():
    assert looks_like_covenant("Financial Covenants: Minimum Debt Yield: 9.0%;")
    assert looks_like_covenant("Max. Debt/EBITDA: 5.0x")
