"""Spreadsheet and CSV statements: the value comes from the right column (statement stress test, 8 November 2026).

Before, the first number after the label was taken: the "Note" column, or last year where it is printed first.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from dps.extraction.statement import extract_values, rows_from_csv, rows_from_xlsx

INDIAN = (b"Statement of Profit and Loss,,,\nAmounts in INR lakhs,,,\nParticulars,Note,31 March 2025,31 March 2024\n"
          b"Revenue from operations,23,\"5,000.00\",\"4,500.00\"\nFinance costs,26,120.5,110\n"
          b"Profit before tax,,800,700\nTotal equity,,2000,1900\n")


def test_the_note_column_is_never_the_value():
    rows = rows_from_csv(INDIAN)
    assert rows == [("Revenue from operations", 5000.0, None, "R4C3"), ("Finance costs", 120.5, None, "R5C3"),
                    ("Profit before tax", 800.0, None, "R6C3"), ("Total equity", 2000.0, None, "R7C3")]
    got = {v.key: v.value for v in extract_values(rows, "Amounts in INR lakhs").values}
    assert got["REVENUE"] == 50.0 and got["INTEREST_PAID"] == 1.205 and got["NET_WORTH"] == 20.0


def test_the_latest_year_is_read_where_the_earlier_year_comes_first():
    assert rows_from_csv(b"Particulars,2024,2025\nRevenue,100,200\nNet profit,10,20\n") == [
        ("Revenue", 200.0, None, "R2C3"), ("Net profit", 20.0, None, "R3C3")]


def test_a_simple_two_column_sheet_still_reads():
    assert rows_from_csv(b"Revenue,500\nEBITDA,50\n") == [("Revenue", 500.0, None, "R1C2"), ("EBITDA", 50.0, None,
                                                                                           "R2C2")]


def test_group_and_company_columns_follow_the_bank_setting():
    wb = Workbook()
    ws = wb.active
    ws.title = "PL"
    for r in [("Rs'000", None, None, None, None), (None, "Group", None, "Company", None),
              ("Particulars", 2025, 2024, 2025, 2024), ("Revenue", 5186, 5054, 5068, 4978), ("Profit before tax", 619,
                                                                                               331, 611, 324)]:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    assert rows_from_xlsx(buf.getvalue())[0] == ("Revenue", 5068.0, None, "PL!D4")
    assert rows_from_xlsx(buf.getvalue(), "CONSOLIDATED")[0] == ("Revenue", 5186.0, None, "PL!B4")
