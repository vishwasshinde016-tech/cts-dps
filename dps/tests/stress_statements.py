"""Statement wording stress test: the ways real statements label each covenant input, and look-alikes that must not
be taken for it.

Labels come from Indian (Schedule III / Ind AS), Sri Lankan (SLFRS), US GAAP and IFRS statements, with the usual
decorations: note numbers, "(refer note 12)", roman numbering, "&", "(Loss)/Profit", trailing colons.
Outcome of a label: right (matched to its line), unmatched (DPS does not take it; the reviewer sees the line as not
found), wrong (matched to another line: a wrong figure that can pass unseen). Look-alikes must be unmatched or
matched to their own line.

  python tests/stress_statements.py
"""

from __future__ import annotations

import itertools
import sys

from dps.extraction.statement import match_key

LABELS: dict[str, list[str]] = {
    "REVENUE": ["Revenue from operations", "Revenue from Operations", "Revenue", "Net sales", "Net Sales",
                "Total revenue", "Turnover", "Sales", "Revenue from contracts with customers", "Total net sales",
                "Net revenues", "Operating revenue", "Total revenues", "Gross revenue", "Income from operations",
                "Net sales and revenues", "Sales revenue"],
    "PBT": ["Profit before tax", "Profit/(Loss) before tax", "(Loss)/Profit before tax", "Profit before income tax",
            "Profit before taxation", "Income before income taxes", "Earnings before income taxes",
            "Profit Before Income Tax Expense", "Loss before tax", "Profit before tax for the year",
            "Income from continuing operations before income taxes", "PBT"],
    "PAT": ["Profit for the year", "Profit/(Loss) for the year", "Net profit", "Net income", "Net Income",
            "Profit after tax", "Net profit for the year", "Loss for the year", "Profit for the period",
            "Net earnings", "Net income (loss)", "Profit after taxation", "Net profit after tax", "PAT"],
    "INTEREST_PAID": ["Finance costs", "Finance cost", "Interest expense", "Interest Expense", "Finance charges",
                      "Interest and finance charges", "Interest expense, net", "Borrowing costs", "Financial expenses",
                      "Interest on borrowings", "Finance expenses"],
    "DEPRECIATION": ["Depreciation and amortisation expense", "Depreciation and amortization",
                     "Depreciation & amortisation", "Depreciation", "Depreciation, depletion and amortization",
                     "Depreciation and amortisation"],
    "TOTAL_ASSETS": ["Total assets", "TOTAL ASSETS", "Total Assets"],
    "CURRENT_ASSETS": ["Total current assets", "Total Current Assets", "Current assets, total"],
    "CURRENT_LIABILITIES": ["Total current liabilities", "Total Current Liabilities"],
    "NET_WORTH": ["Total equity", "Total Equity", "Net worth", "Shareholders' funds", "Total shareholders' equity",
                  "Total stockholders' equity", "Total equity attributable to owners", "Total Shareholders Funds",
                  "Equity attributable to equity holders of the parent"],
    "LT_DEBT": ["Long-term borrowings", "Long term borrowings", "Borrowings (non-current)", "Non-current borrowings",
                "Long-term debt", "Long-term debt, net", "Long-term debt, less current portion",
                "Interest bearing borrowings (non-current)"],
    "ST_DEBT": ["Short-term borrowings", "Short term borrowings", "Borrowings (current)", "Current borrowings",
                "Short-term debt"],
    "CASH": ["Cash and cash equivalents", "Cash & cash equivalents", "Cash and bank balances", "Cash and equivalents",
             "Cash at bank and in hand", "Cash and cash equivalents at end of the year", "Cash"],
    "CFO": ["Net cash from operating activities", "Net cash generated from operating activities",
            "Net cash flow from operating activities", "Net cash provided by operating activities",
            "Net cash (used in)/generated from operating activities", "Cash provided by operations",
            "Net Cash from Operating Activities", "Net cash inflow from operating activities",
            "Net cash provided by (used in) operating activities"],
    "INVENTORY": ["Inventories", "Inventory", "Stock-in-trade", "Inventories, net", "Stocks"],
    "RECEIVABLES": ["Trade receivables", "Trade and other receivables", "Accounts receivable",
                    "Accounts receivable, net", "Sundry debtors", "Debtors", "Trade debtors"],
    "TRADE_PAYABLES": ["Trade payables", "Trade and other payables", "Accounts payable", "Sundry creditors",
                       "Creditors"],
    "CAPEX": ["Purchase of property, plant and equipment", "Capital expenditure", "Purchases of property and equipment",
              "Acquisition of property, plant and equipment", "Payments for property, plant and equipment",
              "Additions to property, plant and equipment"],
    "TAX": ["Tax expense", "Total tax expense", "Income tax expense", "Provision for income taxes", "Income taxes",
            "Income tax"],
}
# Look-alikes: label -> the only acceptable key (None: must not be matched at all).
LOOKALIKES: dict[str, str | None] = {
    "Other current assets": None, "Other current liabilities": None, "Total non-current assets": None,
    "Non-current assets": None, "Current maturities of long-term debt": None,
    "Current portion of long-term borrowings": None, "Profit before exceptional items and tax": None,
    "Interest income": None, "Finance income": None, "Deferred tax": None, "Deferred tax liabilities (net)": None,
    "Earnings per share": None, "Basic earnings per share": None, "Share of profit of associates": None,
    "Net income attributable to noncontrolling interests": None, "Proceeds from long-term borrowings": None,
    "Repayment of long-term borrowings": None, "Cash and cash equivalents at the beginning of the year": None,
    "Net increase in cash and cash equivalents": None, "Total comprehensive income for the year": None,
    "Other comprehensive income": None, "Revenue grants": None, "Deferred revenue": None,
    "Interest paid": "INTEREST_PAID", "Changes in inventories of finished goods": None,
    "Provision for doubtful debts": None, "Gross profit": None,
    "Total equity and liabilities": "TOTAL_EQUITY_AND_LIABILITIES",
    "Total liabilities": None, "Impairment of trade receivables": None, "Increase in trade receivables": None,
    "Lease liabilities": "LEASE_LIABILITIES", "Total income": "TOTAL_INCOME", "Other income": "OTHER_INCOME",
}
DECORATIONS = ["{}", "{} ", "(ii) {}", "3. {}", "{} (refer note 12)", "{}:", "{} 14", "a) {}"]


def results() -> dict:
    out = {"right": 0, "unmatched": 0, "wrong": 0, "lookalike_ok": 0, "lookalike_wrong": 0, "bad": []}
    for key, labels in LABELS.items():
        for label, deco in itertools.product(labels, DECORATIONS):
            got = match_key(deco.format(label))
            if got == key:
                out["right"] += 1
            elif got is None:
                out["unmatched"] += 1
                out["bad"].append(("unmatched", key, deco.format(label)))
            else:
                out["wrong"] += 1
                out["bad"].append((f"wrong:{got}", key, deco.format(label)))
    for label, allowed in LOOKALIKES.items():
        for deco in DECORATIONS[:4]:
            got = match_key(deco.format(label))
            if got is None or got == allowed:
                out["lookalike_ok"] += 1
            else:
                out["lookalike_wrong"] += 1
                out["bad"].append((f"lookalike:{got}", allowed, deco.format(label)))
    return out


if __name__ == "__main__":
    r = results()
    print({k: v for k, v in r.items() if k != "bad"})
    seen = set()
    for kind, key, label in r["bad"]:
        base = (kind, key, label.split("(")[0][-40:])
        if base in seen and len(sys.argv) < 2:
            continue
        seen.add(base)
        print(f"  {kind:28} {str(key):20} {label}")
