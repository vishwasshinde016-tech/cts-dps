"""Sri Lanka run (10 November 2026): 25 CSE interim statements and 25 Sri Lanka-style facility agreements.
Each test was a figure or covenant read wrongly, or not at all, before its fix."""

from __future__ import annotations

import pytest
from test_statement_pdf import amounts, header

from dps.extraction.agreement import extract_terms
from dps.extraction.statement import _unit, match_key
from dps.extraction.statement_pages import order_pages, rank
from dps.extraction.statement_pdf import _text_words, choose_column, rows_from_words, unit_text
from dps.parse.engine import Page

# ---------------------------------------------------------------- statements: the unit

def test_a_unit_stated_in_a_footnote_under_the_table():
    # John Keells Holdings: "Note : All values are in Rs '000s, unless otherwise stated." at the foot of a long
    # page, past the heading text; every amount was read as crore and held as UNIT_NOT_STATED.
    page = "CONSOLIDATED STATEMENT OF FINANCIAL POSITION\nAs at 30.09.2026 31.03.2026\n" + "Line 1,234 5,678\n" * 120
    page += "Note : All values are in Rs '000s, unless otherwise stated.\nThe above figures are not audited."
    assert _unit(unit_text(page)) == "thousand"


def test_a_bare_currency_over_the_columns_still_reads_the_footnote():
    # Hemas: "LKR" over the columns and "All values are in LKR'000s" at the foot; read as plain rupees, every
    # amount 1,000 times too small, and published.
    page = "CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n2026 2025\nLKR LKR\n" + "Revenue 37,791,545 32,286,945\n" * 80
    page += "All values are in LKR'000s, unless otherwise stated."
    assert _unit(unit_text(page)) == "thousand"


def test_a_unit_under_the_heading_wins_over_a_later_note():
    page = "Statement of financial position (all amounts in Sri Lanka Rupees millions)\n" + "x 1\n" * 600
    page += "Figures in Rs '000s unless otherwise stated"
    assert _unit(unit_text(page)) == "million"


# ---------------------------------------------------------------- statements: which line

def test_revenue_reserves_are_equity_not_revenue():
    # Lanka Milk Foods, Asian Hotels, John Keells Hotels, Carsons: "Revenue reserves" was read as the revenue.
    assert match_key("Revenue reserves") is None
    assert match_key("Revenue Reserves") is None
    assert match_key("Revenue") == "REVENUE"


def test_an_amount_spaced_out_character_by_character_keeps_its_thousands():
    # Sunshine: "5" "5" "3" "," "0" "8" "3" was read as 553.
    class FakePage:
        def extract_words(self, x_tolerance):
            chars = [("5", 271.0), ("5", 274.3), ("3", 277.5), (",", 280.8), ("0", 282.4), ("8", 285.6), ("3", 288.9)]
            out = [{"text": "Revenue", "x0": 62.6, "x1": 89.6, "top": 150.9}]
            for i, (c, x0) in enumerate(chars):
                x1 = chars[i + 1][1] if i + 1 < len(chars) else x0 + 3.2
                out.append({"text": c, "x0": x0, "x1": x1, "top": 151.3})
            return out

    assert [w.text for w in _text_words(FakePage())] == ["Revenue", "553,083"]


# ---------------------------------------------------------------- statements: which page

BS = "Statement of financial position\nTotal assets 1,000 900\nTotal equity 500 450\nTotal liabilities 500 450\n"
PL = "Income statement\nRevenue 2,000 1,800\nProfit before tax 300 250\nProfit for the period 200 180\n"


def test_a_segment_note_is_not_a_statement():
    # Royal Ceramics: a segment note's "Total Revenue" (one segment) replaced the income statement's revenue.
    segment = ("11. Segment Information (Cont..)\nTotal Revenue 2,581,583\nRevenue 2,425,239\n"
               "Profit before tax 225,873\n")
    order, _ = order_pages([BS, PL, segment], short=True)
    assert 2 not in order


def test_a_dollar_translation_is_skipped_when_the_statements_are_in_rupees():
    # Teejay Lanka: the rupee statements are followed by a US dollar convenience translation, read as rupees.
    rupees = "Statement of Financial Position\n(All amounts in Sri Lanka Rupees Thousands)\n" + BS
    dollars = "Statement Of Financial Position\n(All amounts in United States Dollars)\n" + BS + BS
    assert rank([rupees, dollars])["BS"] == [0]


def test_the_company_statements_are_read_together():
    # Sunshine, Asian Hotels, John Keells Hotels: the company income statement with the group balance sheet.
    group_bs = "STATEMENT OF FINANCIAL POSITION - GROUP\n" + BS + BS
    company_bs = "STATEMENT OF FINANCIAL POSITION - COMPANY\n" + BS
    company_pl = "COMPANY INCOME STATEMENT\n" + PL
    assert rank([group_bs, company_bs, company_pl])["BS"][0] == 1
    assert rank([group_bs, company_bs, company_pl], "CONSOLIDATED")["BS"][0] == 0
    # The title found after the table text (Asian Hotels' text layer)
    late = "Interim Condensed Financial Statements\n" + BS * 3 + "COMPANY STATEMENT OF FINANCIAL POSITION"
    group = "Interim Condensed Financial Statements\n" + BS * 3 + "CONSOLIDATED STATEMENT OF FINANCIAL POSITION"
    assert rank([group, late])["BS"][0] == 1
    # The group page is then not read for a line the company lacks (Sunshine's inventory).
    order, _ = order_pages([group_bs, company_bs, company_pl, "STATEMENT OF PROFIT OR LOSS - GROUP\n" + PL], True)
    assert 0 not in order and 3 not in order


# ---------------------------------------------------------------- statements: which column

def test_this_periods_column_when_two_columns_share_the_year():
    # ACL Cables: "30-Jun-26 | 31-Mar-26" and the page-level "Unaudited" read as "audited" took the year-end.
    page = (header(10, ("Company", 440)) + header(20, ("30-Jun-26", 380), ("31-Mar-26", 460), ("Unaudited", 540))
            + amounts(40, "Total assets", ("24,610,019", 380), ("22,559,832", 460))
            + amounts(60, "Total equity", ("17,866,697", 380), ("16,958,588", 460)))
    got = {label: value for label, value, _, _ in rows_from_words([page])}
    assert got["Total assets"] == 24610019.0


def test_the_audited_year_end_is_not_this_periods_balance_sheet():
    # Teejay Lanka: "2026 Unaudited | ... | As at 31 March 2026 Audited".
    headers = {0: "2026 unaudited company", 1: "company as at 30 september 2025 unaudited",
               2: "as at 31 march 2026 audited company"}
    values = {c: [1234567.0 + i for i in range(30)] for c in range(3)}
    assert choose_column(headers, values) == 0


def test_a_change_heading_that_spilled_onto_a_year_column():
    # Watawala: "Six months ended" over two columns and "Change %" merged into the 2026 one; last year was taken.
    headers = {0: "2026 company quarter", 1: "2025 company quarter", 2: "change 2026 company months", 3: "2025 months"}
    values = {c: [2451497.0 + i for i in range(16)] for c in range(4)}
    assert choose_column(headers, values) in (0, 2)


# ---------------------------------------------------------------- agreements

def terms(sentence: str) -> list[tuple]:
    got = extract_terms([Page(5, "Financial Covenants\n" + sentence, "TEXT", 1.0)])
    return [(t.metric, t.operator, t.threshold) for t in got if t.category == "FINANCIAL"]


@pytest.mark.parametrize("sentence, metric, op, value", [
    ("a. Debt Service Cover Ratio to be maintained above 1.25 times.", "DSCR", ">", 1.25),
    ("1. Current ratio should be maintained above 1.5 at all times.", "CURRENT_RATIO", ">", 1.5),
    ("2. Debt to equity should be maintained below 0.5.", "DEBT_TO_EQUITY", "<", 0.5),
    ("3. Interest cover should be maintained above 5.0 times.", "ICR", ">", 5.0),
    ("10.1 Debt/Equity Ratio shall be maintained below 1.25:1.", "DEBT_TO_EQUITY", "<", 1.25),
    ("(a) the Debt Service Coverage Ratio shall be maintained at 1.20 times or above;", "DSCR", ">=", 1.2),
    ("b. Total Liabilities to Tangible Net Worth not to exceed 2.5 times.", "DEBT_TO_NET_WORTH", "<=", 2.5),
    ("(ii) an EBITDA to Interest Expense ratio of a minimum of 6.0 times; and", "ICR", ">=", 6.0),
    ("Gearing Ratio not exceeding 100%;", "DEBT_TO_EQUITY", "<=", 1.0),
    ("11.1 Leverage: Consolidated Total Debt shall not exceed 2.0 times Consolidated EBITDA.", "DEBT_TO_EBITDA",
     "<=", 2.0),
    ("14.3 The Borrower shall ensure that its Total Debt does not exceed 50% of its Total Equity.", "DEBT_TO_EQUITY",
     "<=", 0.5),
    ("8.2 The ratio of total borrowings of the Borrower (on a company basis) to its net worth shall not at any time "
     "exceed 0.50:1.", "DEBT_TO_EQUITY", "<=", 0.5),
])
def test_sri_lankan_covenant_wordings(sentence, metric, op, value):
    assert (metric, op, value) in terms(sentence)


def test_a_ratio_spelled_out_with_its_figures_in_brackets():
    # Dialog: "a Debt Service Coverage Ratio of not less than one point five (1.5) times" was not read.
    assert ("DSCR", ">=", 1.5) in terms("(iii) a Debt Service Coverage Ratio of not less than one point five (1.5) "
                                        "times.")


def test_each_list_item_is_its_own_sentence_for_the_safety_net():
    # Dialog: the interest cover found in (ii) hid the DSCR in (iii) of the same paragraph from the safety net.
    from dps.extraction.safety_net import _sentences

    parts = _sentences("The Borrower shall maintain at all times: (i) a Total Debt to EBITDA ratio of not more than "
                       "2.5 times; (ii) an EBITDA to Interest Expense ratio of a minimum of 6.0 times; and (iii) a "
                       "Debt Yield of not less than nine (9) per cent.")
    assert any(p.startswith("(iii)") for p in parts)


def test_ebitda_is_built_from_profit_before_tax_finance_costs_and_depreciation():
    # Sri Lanka run: no income statement printed EBITDA, so every statement was held as MISSING_LINE_ITEMS: EBITDA.
    from dps.extraction.statement import extract_values

    rows = [("Profit before tax", 1000.0, 1, None), ("Finance costs", -200.0, 1, None),
            ("Depreciation and amortisation", 150.0, 3, None), ("Total equity", 5000.0, 2, None),
            ("Interest bearing borrowings", 900.0, 2, None)]
    result = extract_values(rows, "Rs. '000")
    ebitda = next(v for v in result.values if v.key == "EBITDA")
    assert ebitda.value == 0.135 and ebitda.derived  # 1,350 thousand in crore
    assert not any(i.startswith("MISSING_LINE_ITEMS") and "EBITDA" in i for i in result.issues)


def test_a_stock_provision_is_not_the_inventory():
    # Hemas: the cash flow's "Provision/ (Reversal) for Obsolete & Slow-Moving Stocks" was published as inventory.
    assert match_key("Provision/ (Reversal) for Obsolete & Slow-Moving Stocks") is None
    assert match_key("Inventories") == "INVENTORY"
