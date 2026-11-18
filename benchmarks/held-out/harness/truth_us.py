"""US answer keys for the held-out 10-Ks, from SEC XBRL company facts (no person involved).

The same concepts and output space as the first benchmark (real-documents/harness/us_truth.py): each figure in
the statement's printed unit times DPS's factor for that unit, so a reader that misreads the unit scores wrong.
The unit is read from the heading of the page that holds the balance sheet. A duration figure must cover one
fiscal year (300 to 380 days) of the 10-K's own filing.

  python truth_us.py <us_dir> <us_pdf_dir> <out.json>
"""
import json
import re
import sys
from datetime import date

from pypdf import PdfReader

TO_CRORE = {"million": 0.1, "thousand": 0.0001}
CONCEPTS = {
    "REVENUE": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "PAT": ["NetIncomeLoss", "ProfitLoss"],
    "INTEREST_PAID": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "PBT": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "TAX": ["IncomeTaxExpenseBenefit"],
    "CASH": ["CashAndCashEquivalentsAtCarryingValue"],
    "INVENTORY": ["InventoryNet"],
    "RECEIVABLES": ["AccountsReceivableNetCurrent"],
    "CURRENT_ASSETS": ["AssetsCurrent"],
    "TOTAL_ASSETS": ["Assets"],
    "CURRENT_LIABILITIES": ["LiabilitiesCurrent"],
    "NET_WORTH": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "LT_DEBT": ["LongTermDebtNoncurrent"],
    "TRADE_PAYABLES": ["AccountsPayableCurrent"],
    "CFO": ["NetCashProvidedByUsedInOperatingActivities"],
    "CFI": ["NetCashProvidedByUsedInInvestingActivities"],
    "CFF": ["NetCashProvidedByUsedInFinancingActivities"],
    "CAPEX": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}
INSTANT = {"CASH", "INVENTORY", "RECEIVABLES", "CURRENT_ASSETS", "TOTAL_ASSETS", "CURRENT_LIABILITIES", "NET_WORTH",
           "LT_DEBT", "TRADE_PAYABLES"}


def unit_of(pdf: str) -> tuple[str, int]:
    """The printed unit of the balance sheet: the page naming the balance sheet with total assets on it."""
    for no, page in enumerate(PdfReader(pdf).pages, 1):
        text = (page.extract_text() or "").lower()
        if "balance sheet" in text and "total assets" in text and "total liabilities" in text:
            head = text[:900]
            if re.search(r"in\s+millions|\(millions|millions\s+of", head):
                return "million", no
            if re.search(r"in\s+thousands|\(thousands|thousands\s+of", head):
                return "thousand", no
    return "million", 0


def days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def main(us_dir: str, pdf_dir: str, out: str) -> None:
    index = json.load(open(f"{us_dir}/index.json"))
    truth = {}
    for doc, meta in index.items():
        unit, page = unit_of(f"{pdf_dir}/{doc}.pdf")
        facts = json.load(open(f"{us_dir}/{doc}.facts.json"))["facts"].get("us-gaap", {})
        acc, end = meta["accession"], meta["report_date"]
        vals = {}
        for key, names in CONCEPTS.items():
            cands = []
            for name in names:
                for u in facts.get(name, {}).get("units", {}).get("USD", []):
                    if u.get("accn") != acc or u.get("end") != end:
                        continue
                    if key not in INSTANT and not (u.get("start") and 300 <= days(u["start"], end) <= 380):
                        continue
                    v = u["val"] / (1e6 if unit == "million" else 1e3) * TO_CRORE[unit]
                    cands.append(round(abs(v) if key == "CAPEX" else v, 6))
            if cands:
                vals[key] = sorted(set(cands))
        truth[doc] = {"company": meta["name"], "period_end": end, "unit": unit, "balance_sheet_page": page,
                      "values": vals}
        print(doc, meta["name"], unit, "page", page, len(vals), "keys")
    json.dump(truth, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
