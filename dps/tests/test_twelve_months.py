"""12-month covenants (10 November 2026): a quarter's or half-year's profit is not a year's.

Debt / EBITDA, interest cover, DSCR and minimum EBITDA are written on twelve months. An interim statement's EBITDA
covers three, six or nine; tested as it is, Debt / EBITDA from one quarter is about four times too high. DPS records
the months each figure covers and builds the trailing twelve months: this year to date + last full year - last year
to date.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from test_flows import add_document, database, statement  # noqa: F401 - database is a fixture

from dps import services
from dps.catalog import LINE_ITEMS
from dps.extraction.mapping import DPS_FORMULAS
from dps.extraction.statement_pdf import choose_column, column_months, period_columns, period_months
from dps.models import Document, StatementValue

# ---------------------------------------------------------------- which period a column covers

@pytest.mark.parametrize("header, months", [
    ("03 months ended 30 June 2026", 3), ("Quarter ended 30th September", 3), ("Three months to", 3),
    ("06 months ended 30 June", 6), ("Six months ended 30th September 2026", 6), ("Half year", 6),
    ("Nine months ended 31 December", 9), ("Year ended 31 March 2026 Audited", 12), ("Twelve months to", 12),
    ("As at 30.09.2026", None), ("2026", None),
])
def test_period_months_from_the_heading(header, months):
    assert period_months(header) == months


def test_a_period_heading_over_a_pair_names_both_columns():
    # Ceylon Tobacco: "03 months ended 30 June" sits over "2026 2025" and lands on the 2025 column only.
    headers = {0: "2026 un-audited", 1: "03 months ended 30 june 2025 un-audited",
               2: "06 months ended 30 june 2026 un-audited", 3: "2025 un-audited"}
    assert column_months(headers) == {0: 3, 1: 3, 2: 6, 3: 6}


def test_the_year_to_date_and_last_years_same_period_are_taken():
    headers = {0: "2026 un-audited", 1: "03 months ended 30 june 2025 un-audited",
               2: "06 months ended 30 june 2026 un-audited", 3: "2025 un-audited"}
    values = {c: [1000000.0 + i for i in range(15)] for c in range(4)}
    best = choose_column(headers, values)
    assert best == 2  # the six months to date, not the quarter
    assert period_columns(headers, values, best) == (6, 3, None)


def test_an_interim_with_the_last_full_year_printed():
    # Dipped Products: "Year to 31/03/2026 Audited" on the left of "03 months to 30/06/2026".
    headers = {0: "year to 31/03/2026 audited", 1: "03 months to 30/06/2026 unaudited",
               2: "03 months to 30/06/2025 unaudited"}
    values = {c: [5000000.0 + i for i in range(12)] for c in range(3)}
    best = choose_column(headers, values)
    assert best == 1  # this quarter, not the old full year
    assert period_columns(headers, values, best) == (3, 2, 0)


def test_a_full_year_column_must_end_within_the_last_twelve_months():
    # Sunshine: last year's same period ("30 September 2025") is not a full year.
    headers = {0: "period ended 30th september 2026", 1: "year ended 30th september 2025"}
    values = {c: [100.0 + i for i in range(10)] for c in range(2)}
    assert period_columns(headers, values, 0)[2] is None


# ---------------------------------------------------------------- the twelve-month figures

def put(db, cp, key, value, as_of, months, source="DOCUMENT", derived=False, unit="INR_CR"):
    with db.session_factory() as s:
        s.add(StatementValue(cp_id=cp, key=key, value=value, unit=unit, currency=unit[:3], as_of_date=as_of,
                             period_months=months, derived=derived, source_type=source))
        s.commit()


def build(db, doc_id):
    with db.session_factory() as s:
        doc = s.get(Document, doc_id)
        made = {r.key: (round(r.value, 6), r.note) for r in services.build_twelve_months(s, doc, "CP-1", 3)}
        s.commit()
        return made


def test_ttm_from_the_year_to_date_the_last_full_year_and_last_years_same_period(database):  # noqa: F811
    doc = add_document(database, "FINANCIAL_STATEMENT", "h1.pdf", date(2026, 9, 30), ("CP-1",))
    put(database, "CP-1", "EBITDA", 1100.0, date(2026, 3, 31), 12)  # the annual statement
    put(database, "CP-1", "EBITDA", 500.0, date(2025, 9, 30), 6, source="COMPARATIVE")  # beside this period's
    put(database, "CP-1", "EBITDA", 600.0, date(2026, 9, 30), 6)
    made = build(database, doc)
    assert made["EBITDA_TTM"][0] == 1200.0  # 600 + 1,100 - 500
    assert "6 months to 2026-09-30 + year to 2026-03-31 - 6 months to 2025-09-30" in made["EBITDA_TTM"][1]
    assert made["EBITDA_ANN"][0] == 1200.0  # 600 x 12 / 6


def test_figures_in_another_money_unit_are_never_added(database):  # noqa: F811
    """Money for any country: last year kept in INR crore and this half-year in LKR million make no 12 months."""
    doc = add_document(database, "FINANCIAL_STATEMENT", "h1.pdf", date(2026, 9, 30), ("CP-1",))
    put(database, "CP-1", "EBITDA", 1100.0, date(2026, 3, 31), 12, unit="INR_CR")
    put(database, "CP-1", "EBITDA", 500.0, date(2025, 9, 30), 6, source="COMPARATIVE", unit="INR_CR")
    put(database, "CP-1", "EBITDA", 600.0, date(2026, 9, 30), 6, unit="LKR_MN")
    made = build(database, doc)
    assert "EBITDA_TTM" not in made and made["EBITDA_ANN"][0] == 1200.0


def test_a_first_quarter_uses_last_years_first_quarter(database):  # noqa: F811
    doc = add_document(database, "FINANCIAL_STATEMENT", "q1.pdf", date(2026, 6, 30), ("CP-1",))
    put(database, "CP-1", "INTEREST_PAID", 40.0, date(2026, 3, 31), 12)
    put(database, "CP-1", "INTEREST_PAID", 9.0, date(2025, 6, 30), 3, source="COMPARATIVE")
    put(database, "CP-1", "INTEREST_PAID", 11.0, date(2026, 6, 30), 3)
    assert build(database, doc)["INTEREST_PAID_TTM"][0] == 42.0


def test_a_full_year_is_its_own_twelve_months(database):  # noqa: F811
    doc = add_document(database, "FINANCIAL_STATEMENT", "fy.pdf", date(2026, 3, 31), ("CP-1",))
    put(database, "CP-1", "PAT", 300.0, date(2026, 3, 31), None)  # months not stated: March is the year-end
    assert build(database, doc)["PAT_TTM"][0] == 300.0


def test_no_twelve_month_figure_without_the_last_full_year(database):  # noqa: F811
    # The covenant is then "could not be checked", never tested on six months.
    doc = add_document(database, "FINANCIAL_STATEMENT", "h1.pdf", date(2026, 9, 30), ("CP-1",))
    put(database, "CP-1", "EBITDA", 500.0, date(2025, 9, 30), 6, source="COMPARATIVE")
    put(database, "CP-1", "EBITDA", 600.0, date(2026, 9, 30), 6)
    made = build(database, doc)
    assert "EBITDA_TTM" not in made and made["EBITDA_ANN"][0] == 1200.0


def test_months_not_stated_are_counted_from_the_financial_year():
    assert services.months_covered(None, date(2026, 9, 30), 3) == 6
    assert services.months_covered(None, date(2026, 6, 30), 3) == 3
    assert services.months_covered(None, date(2026, 6, 30), 12) == 6  # a December year (Ceylon Tobacco)
    assert services.months_covered(9, date(2026, 6, 30), 3) == 9  # what the statement says wins


def test_comparative_columns_are_never_served_to_core(database):  # noqa: F811
    put(database, "CP-1", "EBITDA", 500.0, date(2025, 9, 30), 6, source="COMPARATIVE")
    with database.session_factory() as s:
        served = services.values_series(s, "CP-1", ["EBITDA"], date(2026, 12, 31), None)
    assert served["points"] == [] and served["missingKeys"] == ["EBITDA"]


def test_comparatives_never_replace_a_periods_own_statement(database):  # noqa: F811
    from dps.extraction.statement import LineValue

    put(database, "CP-1", "PAT", 210.0, date(2025, 9, 30), 6)  # last year's own statement
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "h1.pdf", date(2026, 9, 30), ("CP-1",))
    with database.session_factory() as s:
        doc = s.get(Document, doc_id)
        services.store_comparatives(s, doc, "CP-1", [LineValue("PAT", 205.0, "INR_CR")],
                                    [LineValue("PAT", 400.0, "INR_CR")], "2026-03", 6)
        s.commit()
        rows = {(r.as_of_date, r.source_type): r.value for r in s.scalars(select(StatementValue).where(
            StatementValue.key == "PAT", StatementValue.superseded.is_(False)))}
    assert rows == {(date(2025, 9, 30), "DOCUMENT"): 210.0, (date(2026, 3, 31), "COMPARATIVE"): 400.0}


# ---------------------------------------------------------------- what Core tests

def test_earnings_covenants_are_tested_on_twelve_months():
    for metric in ("DSCR", "ICR", "DEBT_TO_EBITDA", "NET_LEVERAGE_RATIO", "EBITDA_TO_EMI", "MIN_EBITDA",
                   "FIXED_CHARGE_COVERAGE_RATIO"):
        assert "_TTM" in DPS_FORMULAS[metric], metric
    assert DPS_FORMULAS["CURRENT_RATIO"] == "DPS_CURRENT_ASSETS / DPS_CURRENT_LIABILITIES"  # balances as they are
    assert {"EBITDA_TTM", "EBITDA_ANN", "INTEREST_PAID_TTM", "PRINCIPAL_PAID_TTM"} <= LINE_ITEMS.keys()


def test_a_reported_half_year_statement_builds_its_twelve_months(database):  # noqa: F811
    # End to end through Flow B: the annual figure is stored first, then the half year with last year's column.
    put(database, "CP-1", "EBITDA", 1100.0, date(2026, 3, 31), 12)
    put(database, "CP-1", "EBITDA", 500.0, date(2025, 9, 30), 6)
    csv = (b"Amounts in INR crore,30 September 2026\nEBITDA,600\nShort-term borrowings,300\n"
           b"Long-term borrowings,700\nTotal equity,2000\n")
    flow, doc_id = statement(database, csv, "h1.csv", as_of=date(2026, 9, 30))
    assert flow.state.outcome == "REPORTED"
    with database.session_factory() as s:
        ttm = s.scalar(select(StatementValue).where(StatementValue.key == "EBITDA_TTM",
                                                     StatementValue.superseded.is_(False)))
    assert ttm is not None and ttm.value == 1200.0 and ttm.derived and ttm.period_months == 12
