"""Phase 5 of "never miss silently": each error the held-out set (documents DPS had never seen) found, as a test.

outputs/benchmarks/held-out/README.md has the measurement; every case here was a figure or covenant that reached
the output wrong with no flag, or a quote a reviewer could not check against.
"""

from __future__ import annotations

from dps.extraction.agreement import _defined_numbers, _Doc, extract_terms
from dps.extraction.statement import _unit, extract_values, match_key
from dps.extraction.statement_pages import order_pages, rank
from dps.parse.engine import Page


def test_a_long_paragraph_quotes_the_covenant_not_the_end_of_the_paragraph():
    # HA02: an HTML filing has no line breaks inside a paragraph; the quote was the paragraph's last 300 characters.
    text = ("x " * 400 + "Section 8.17 Financial Covenants. Borrower shall cause Guarantor to maintain a consolidated "
            "net worth of not less than $50,000,000.00 as determined by Lender. " + "lockbox " * 300)
    [term] = [t for t in extract_terms([Page(186, text, "TEXT", 1.0)]) if t.category == "FINANCIAL"]
    assert term.threshold == 50_000_000 and "net worth of not less than $50,000,000.00" in term.quote
    assert term.quote.index("net worth") < 150  # the covenant is near the start, not cut away


def test_a_number_that_counts_something_else_is_not_a_limit():
    # HA09: "Fixed Charge Coverage Ratio >= 9" from "to at least 9 decimals" in an assignment form.
    text = ("the Fixed Charge Coverage Ratio of the Borrower. Set forth, to at least 9 decimals, as a percentage of "
            "the Commitment of all Banks thereunder.")
    assert [t for t in extract_terms([Page(131, text, "TEXT", 1.0)]) if t.category == "FINANCIAL"] == []


def test_a_statement_in_hundreds_of_rupees():
    # HI02: "Rs. In Hundreds" was read as plain rupees, every amount 100 times too small.
    assert _unit("Reliance SED Limited Balance Sheet as at March 31, 2025 Rs. In Hundreds") == "hundred"
    result = extract_values([("Total Assets", 1068.83, 14, None)], "Rs. In Hundreds")
    assert result.values[0].value == round(1068.83 * 100 / 1e7, 6)


def test_cash_flow_lines_are_not_balances_or_totals():
    # HL01: "Proceeds from Long Term Borrowings" was long-term debt; "Cash Generated from Operations" (before
    # interest, tax and gratuity paid) was the operating cash flow although the net line followed.
    assert match_key("Proceeds from Long Term Borrowings") is None
    assert match_key("Repayments of Long Term Borrowings") != "LT_DEBT"
    rows = [("Cash Generated from Operations", 1202826.0, 150, None), ("Interest Paid", -425285.0, 150, None),
            ("Net Cash from Operating Activities", 455795.0, 150, None)]
    cfo = {v.key: v.value for v in extract_values(rows, "Rs`000").values}["CFO"]
    assert cfo == round(455795 * 0.0001, 6)
    # A statement that prints only "Cash generated from operations" still has an operating cash flow.
    only = extract_values([("Cash generated from operations", 100.0, 3, None)], "Rs. in crore")
    assert {v.key for v in only.values} >= {"CFO"}


def test_a_share_of_associates_income_is_not_net_income():
    # HU02: "Share of net income of equity companies" (196) was taken as the net income (2,021).
    assert match_key("Share of net income of equity companies") is None
    assert match_key("Net income attributable to noncontrolling interests") is None
    assert match_key("Net Income") == "PAT"


def test_a_balance_sheet_on_two_pages_is_read_whole():
    # HL01: the liabilities and their total were on the next page; a segment note's "Current liabilities" won.
    filler = "Rs`000 " + " ".join(f"{n},{n:03d}" for n in range(100, 160))
    texts = ["Annual report " * 40,
             "Consolidated Statement of Financial Position As at 31/03/2025 ASSETS Non Current Assets Inventories "
             "Trade and Other Receivables Total Current Assets Total Assets Stated Capital Total Equity " + filler,
             "Non Current Liabilities Interest Bearing Borrowings Current Liabilities Trade & Other Payables "
             "Total Current Liabilities Total Liabilities Total Equity and Liabilities " + filler,
             "Statement of Cash Flows operating activities investing activities financing activities " + filler]
    assert rank(texts)["BS"][0] == 1
    order, main = order_pages(texts, short=False)
    assert order[:2] == [1, 2] and main >= 3
    # A balance sheet that ends on its own page is not followed by the next page.
    texts[1] += " Total Liabilities Total Equity and Liabilities"
    order, _ = order_pages(texts, short=False)
    assert 2 not in order[:2]


def test_covenants_written_as_a_prohibited_state_reach_a_person():
    # HA04, HA08, HA01: no rule read them and the safety net did not show them either.
    from dps.extraction.safety_net import looks_like_covenant

    assert looks_like_covenant("(2) the Borrower shall not permit the Interest Coverage Ratio as of the last day of "
                               "each Test Period to be less than 2.50 to 1.00.")
    assert looks_like_covenant("(f) the Borrower to have a ratio of the Borrower's EBITDA to the Borrower's Interest "
                               "Expense with respect to (i) any Indebtedness under this Agreement, (ii) any "
                               "Indebtedness under the Margin Facility and (iii) any other Subordinated "
                               "Indebtedness, as of the last day of each of Borrower's fiscal quarters, of less than "
                               "1.15 to 1.0, calculated for the twelve month period.")
    assert looks_like_covenant("Guarantor (in the aggregate) fails to maintain at all times Liquid Assets of at least "
                               "$1,500,000.00, to be tested pursuant to the financial statements.")
    # A state with no covenant word is still not a covenant.
    assert not looks_like_covenant("the Borrower to have paid fees of less than $5,000 to the Agent on any day.")


# HA16 (a railcar term loan): the covenant sentences hold no number, the definitions do.
_DEFINITIONS = ("“DSCR Failure” has the meaning set forth in Section 7.12(b)(i). “DSCR Minimum” means one "
                "point one five to one (1.15:1.00). “LTV Cash Sweep Ratio Threshold” means 71.0%. “LTV Maximum "
                "Ratio” means 73.0%. “Margin” means the sum of 2.00% and the Applicable Spread.")
_COVENANTS = ("SECTION 7.12 Financial Ratios and Tests. (a) Loan-to-Value Ratio. (i) If the Loan-to-Value Ratio on any "
              "LTV Test Date exceeds the LTV Maximum Ratio (a “LTV Breach”), a “LTV Trigger Event” shall have "
              "occurred. If the Loan-to-Value Ratio on any LTV Test Date is greater than the LTV Cash Sweep Ratio "
              "Threshold but less than or equal to the LTV Maximum Ratio, a “LTV Cash Sweep Event” shall have "
              "occurred. (b) Debt Service Coverage Ratio. (i) If the DSCR on any DSCR Test Date is less than the DSCR "
              "Minimum, a “DSCR Failure” shall have occurred.")


def test_a_threshold_given_by_a_defined_word_is_read_and_flagged():
    terms = [t for t in extract_terms([Page(22, _DEFINITIONS, "TEXT", 1.0), Page(106, _COVENANTS, "TEXT", 1.0)])
             if t.category == "FINANCIAL"]
    got = {(t.metric, t.operator, t.threshold, t.page) for t in terms}
    assert got == {("LOAN_TO_VALUE", "<=", 73.0, 106), ("DSCR", ">=", 1.15, 106)}
    # A person always checks it, and sees where the number came from.
    for t in terms:
        assert any(i.startswith("THRESHOLD_FROM_DEFINITION") for i in t.issues) and t.confidence < 0.85
    assert "exceeds the LTV Maximum Ratio" in next(t.quote for t in terms if t.metric == "LOAN_TO_VALUE")


def test_a_definition_is_not_a_covenant_and_a_formula_is_not_a_number():
    # Before: "'LTV Cash Sweep Ratio Threshold' means 71.0%" was itself proposed as an LTV covenant of 71%.
    assert [t for t in extract_terms([Page(33, _DEFINITIONS, "TEXT", 1.0)]) if t.category == "FINANCIAL"] == []
    defined = _defined_numbers(_Doc([Page(22, _DEFINITIONS, "TEXT", 1.0)]))
    assert set(defined) == {"DSCR Minimum", "LTV Maximum Ratio"}  # no cash sweep trigger, no "Margin" formula


def test_a_ratio_in_words():
    # HA10: "a Liability to Equity Ratio of not greater than one-to-one".
    from dps.extraction.safety_net import looks_like_covenant

    text = ("6.5 Debt to Equity Ratio. Borrower shall at all times maintain a Debt to Equity Ratio of not greater than "
            "one-to-one, as reflected on its annual Financial Statements.")
    [term] = [t for t in extract_terms([Page(36, text, "TEXT", 1.0)]) if t.category == "FINANCIAL"]
    assert (term.metric, term.operator, term.threshold) == ("DEBT_TO_EQUITY", "<=", 1.0)
    assert looks_like_covenant("Borrower shall at all times maintain a Liability to Equity Ratio of not greater than "
                               "one-to-one, as reflected on its annual and quarterly Financial Statements.")
    assert not looks_like_covenant("The Borrower shall deliver one to one hundred copies of each report.")


def test_opening_cash_is_not_the_cash_figure():
    # HI02 (AI run): "Cash and Cash Equivalents as at the beginning of the year" (926.40) was read as the cash;
    # the year-end figure is 420.38.
    assert match_key("Cash and Cash Equivalents as at the beginning of the year") is None
    assert match_key("Cash and Cash Equivalents as at end of the year") == "CASH"
    assert match_key("Cash and cash equivalents") == "CASH"


# Kotagala's profit and loss: "Group" and "Company" each sit over a pair of columns (2025, 2024).
_KOTAGALA = {0: "Notes", 1: "2025 Rs`000", 2: "Group 2024 Rs`000", 3: "2025 Rs`000", 4: "Company 2024 Rs`000"}
_KOTAGALA_VALUES = {0: [5.0, 7.0], 1: [5186237.0, 919718.0], 2: [5054882.0, 672095.0], 3: [5068576.0, 903500.0],
                    4: [4978624.0, 655418.0]}


def test_the_bank_s_entity_column_is_read_when_group_and_company_sit_side_by_side():
    from dps.extraction.statement_pdf import choose_column

    # Before: the two 2025 columns had no entity word, tied, and the group (first) one was read.
    assert choose_column(_KOTAGALA, _KOTAGALA_VALUES) == 3
    assert choose_column(_KOTAGALA, _KOTAGALA_VALUES, "CONSOLIDATED") == 1
    # A page with one entity only is unaffected.
    assert choose_column({1: "2025", 2: "2024"}, {1: [1.0, 2.0], 2: [3.0, 4.0]}) == 1


def test_the_second_reader_is_told_the_bank_s_entity_and_definitions():
    from dps.extraction.statement_ai import prompt

    text = prompt("=== Page 1 ===\nRevenue 10", "STANDALONE")
    assert "use the standalone (company) ones" in text
    assert "net if the statement prints it net" in text and "including non-controlling" in text
    assert "at the END of the year" in text
    assert "use the consolidated (group) ones" in prompt("x", "CONSOLIDATED")


def test_net_worth_is_the_total_with_minority_interests():
    # HU02: "Total Kimberly-Clark Corporation Stockholders' Equity" (1,502) was read; the total is 1,630.
    rows = [("Total Kimberly-Clark Corporation Stockholders' Equity", 1502.0, 47, None),
            ("Noncontrolling interests", 128.0, 47, None), ("Total Stockholders' Equity", 1630.0, 47, None),
            ("Total Liabilities and Stockholders' Equity", 17098.0, 47, None)]
    worth = {v.key: v.value for v in extract_values(rows, "(Millions of dollars)").values}["NET_WORTH"]
    assert worth == 163.0


def test_a_clause_with_several_levels_is_flagged():
    # Wording stress test: "4.00 to 1.00 ..., and thereafter 3.50 to 1.00" gave 4.00 with no flag.
    text = ("During the Initial Period the Borrower shall maintain a Leverage Ratio of not more than 4.00 to 1.00, "
            "and thereafter the Borrower shall maintain a Leverage Ratio of not more than 3.50 to 1.00.")
    [term] = [t for t in extract_terms([Page(3, text, "TEXT", 1.0)]) if t.category == "FINANCIAL"]
    assert (term.threshold, term.issues) == (4.0, ["SEVERAL_LEVELS: the clause also gives 3.5"])
    [one] = [t for t in extract_terms([Page(3, "The Borrower shall maintain a Leverage Ratio of not more than 4.00 to "
                                                "1.00.", "TEXT", 1.0)]) if t.category == "FINANCIAL"]
    assert one.issues == []
