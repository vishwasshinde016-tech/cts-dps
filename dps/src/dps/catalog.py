"""Statement line items DPS serves to Core (04-api-contracts.md §6.2)."""

from __future__ import annotations

LINE_ITEMS: dict[str, str] = {
    "REVENUE": "Revenue",
    "EBITDA": "EBITDA",
    "EBIT": "EBIT",
    "DEPRECIATION": "Depreciation and amortisation",
    "INTEREST_PAID": "Interest paid",
    "PRINCIPAL_PAID": "Principal repaid",
    "EMI_AMOUNT": "EMI amount",
    "PAT": "Profit after tax",
    "TOTAL_DEBT": "Total debt",
    "ST_DEBT": "Short-term debt",
    "LT_DEBT": "Long-term debt",
    "NET_DEBT": "Net debt",
    "CASH": "Cash and equivalents",
    "NET_WORTH": "Net worth",
    "TANGIBLE_NET_WORTH": "Tangible net worth",
    "CURRENT_ASSETS": "Current assets",
    "CURRENT_LIABILITIES": "Current liabilities",
    "INVENTORY": "Inventory",
    "RECEIVABLES": "Receivables",
    "CAPEX": "Capital expenditure",
    # C6: the rest of a full spread.
    "OTHER_INCOME": "Other income",
    "TOTAL_INCOME": "Total income",
    "MATERIAL_COST": "Cost of materials",
    "EMPLOYEE_COST": "Employee costs",
    "OTHER_EXPENSES": "Other expenses",
    "TOTAL_EXPENSES": "Total expenses",
    "PBT": "Profit before tax",
    "TAX": "Tax expense",
    "FIXED_ASSETS": "Property, plant and equipment",
    "CWIP": "Capital work in progress",
    "INTANGIBLES": "Intangible assets",
    "INVESTMENTS": "Investments",
    "TOTAL_ASSETS": "Total assets",
    "SHARE_CAPITAL": "Share capital",
    "RESERVES": "Reserves (other equity)",
    "TRADE_PAYABLES": "Trade payables",
    "PROVISIONS": "Provisions",
    "LEASE_LIABILITIES": "Lease liabilities",
    "TOTAL_EQUITY_AND_LIABILITIES": "Total equity and liabilities",
    "CFO": "Cash from operating activities",
    "CFI": "Cash from investing activities",
    "CFF": "Cash from financing activities",
    "DIVIDENDS_PAID": "Dividends paid",
}


# 12-month covenants (10 November 2026): the trailing twelve months and the annualised figure of each period line.
for _key in ("REVENUE", "EBITDA", "EBIT", "PBT", "PAT", "INTEREST_PAID", "DEPRECIATION", "PRINCIPAL_PAID", "EMI_AMOUNT",
             "CFO", "CAPEX"):
    LINE_ITEMS[_key + "_TTM"] = LINE_ITEMS[_key] + ", last 12 months"
    LINE_ITEMS[_key + "_ANN"] = LINE_ITEMS[_key] + ", annualised"


def catalog() -> dict:
    return {"columns": [{"key": k, "label": v, "dataType": "NUMBER", "kind": "METRIC"}
                        for k, v in LINE_ITEMS.items()]}
