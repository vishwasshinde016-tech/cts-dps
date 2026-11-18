"""Covenant wording stress test: every way of writing a covenant that the benchmarks and real agreements use.

Each case is one agreement page holding one covenant, written with a combination of
- a covenant name (the usual ones and unusual ones DPS has no metric for: debt yield, security cover ...),
- a sentence form (shall maintain, will not permit ... to be less than, list items, numbered clauses, Indian
  "shall be minimum", tables, step-down schedules, defined thresholds),
- a number format (1.25, 1.25x, 1.25 times, 1.25:1, 1.25 to 1.00, 125%, words, currency amounts),
set among ordinary legal text. Plus distractors that hold a ratio and a number but are not covenants (pricing
grids, incurrence tests, baskets, definitions, cash sweeps, representations).

Outcome of a covenant case:
  found         a proposed covenant of the right family, direction and number
  shown         a possible covenant, or a flagged covenant, on the page: a person looks
  silent_wrong  only a covenant with another number or direction and no flag
  missed        nothing on the page
Outcome of a distractor: clean_wrong when it produces a covenant with no flag (the reviewer would trust it).

  python tests/stress_covenants.py          prints the report and the failing cases
"""

from __future__ import annotations

import itertools
import re
import sys
from dataclasses import dataclass
from types import SimpleNamespace

from dps.extraction.agreement import extract_terms
from dps.extraction.coverage import possible_covenants
from dps.extraction.validation import check_direction
from dps.parsing import Page

FAMILY = {"DSCR": "DSCR", "ICR": "ICR", "FIXED_CHARGE_COVERAGE_RATIO": "FCCR", "DEBT_TO_EBITDA": "LEVERAGE",
          "NET_LEVERAGE_RATIO": "LEVERAGE", "DEBT_TO_NET_WORTH": "GEARING", "DEBT_TO_EQUITY": "GEARING",
          "CURRENT_RATIO": "CURRENT", "QUICK_RATIO": "QUICK", "MIN_TANGIBLE_NET_WORTH": "NW", "MIN_NET_WORTH": "NW",
          "MINIMUM_LIQUIDITY": "LIQUIDITY", "EBITDA_TO_EMI": "DSCR", "LOAN_TO_VALUE": "LTV", "MIN_EBITDA": "EBITDA"}

# family -> (direction, kind, names). kind: ratio | percent | amount. Family None: DPS has no metric; it must be
# shown to a person.
COVENANTS = {
    "DSCR": ("min", "ratio", ["Debt Service Coverage Ratio", "DSCR", "Debt Service Cover Ratio",
                              "Consolidated Debt Service Coverage Ratio"]),
    "ICR": ("min", "ratio", ["Interest Coverage Ratio", "Interest Cover", "ICR", "Consolidated Interest Coverage Ratio",
                             "Interest Service Coverage Ratio"]),
    "FCCR": ("min", "ratio", ["Fixed Charge Coverage Ratio", "FCCR", "Consolidated Fixed Charge Coverage Ratio"]),
    "LEVERAGE": ("max", "ratio", ["Total Leverage Ratio", "Leverage Ratio", "Total Debt to EBITDA",
                                  "Net Debt to EBITDA", "Total Net Leverage Ratio", "Senior Secured Leverage Ratio",
                                  "Funded Debt to EBITDA Ratio", "Consolidated Leverage Ratio"]),
    "GEARING": ("max", "ratio", ["Debt to Equity Ratio", "Total Debt to Tangible Net Worth", "Debt to Net Worth Ratio",
                                 "TOL/TNW", "Total Outside Liabilities to Tangible Net Worth", "Gearing Ratio"]),
    "CURRENT": ("min", "ratio", ["Current Ratio"]),
    "NW": ("min", "amount", ["Tangible Net Worth", "Net Worth", "Consolidated Tangible Net Worth",
                             "Adjusted Tangible Net Worth"]),
    "LIQUIDITY": ("min", "amount", ["Liquidity", "Minimum Liquidity", "Unrestricted Cash", "Liquid Assets"]),
    "LTV": ("max", "percent", ["Loan to Value Ratio", "LTV", "Loan-to-Value Ratio"]),
    "EBITDA": ("min", "amount", ["Consolidated EBITDA", "Minimum EBITDA"]),
    None: ("min", "ratio", ["Debt Yield", "Security Cover", "Asset Coverage Ratio", "Collateral Coverage Ratio",
                            "Fixed Assets Coverage Ratio"]),
}

RATIO_VALUES = {"min": [1.25, 1.1, 2.0], "max": [3.5, 2.75, 0.6]}
PERCENT_VALUES = [75.0, 65.5]
AMOUNTS = [(50_000_000, "$50,000,000"), (50_000_000, "$50.0 million"), (50_000_000, "USD 50,000,000"),
           (5_000_000_000, "INR 500 crore"), (5_000_000_000, "Rs. 500 Crores"), (1_000_000_000, "LKR 1,000,000,000")]
WORDS = {1.25: "one and one-quarter", 2.0: "two", 3.5: "three and one-half", 1.1: "one point one",
         2.75: "two and three-quarters", 0.6: "zero point six"}


def ratio_formats(v: float) -> list[str]:
    out = [f"{v:.2f}", f"{v:.2f}x", f"{v:.2f} times", f"{v:.2f}:1", f"{v:.2f}:1.00", f"{v:.2f} to 1",
           f"{v:.2f} to 1.00", f"{v:g} : 1"]
    if v in (2.0, 3.5, 1.25):
        out.append(f"{WORDS[v]} to one")
    return out


def percent_formats(v: float) -> list[str]:
    n = f"{v:g}"
    return [f"{n}%", f"{n} per cent", f"{n} percent", f"{n} per cent. ({n}%)"]


FLOOR_FORMS = [
    "The Borrower shall maintain a {name} of not less than {num}.",
    "The Borrower shall ensure that the {name} is at all times at least {num}.",
    "The Borrower will not permit the {name} as of the last day of any Fiscal Quarter to be less than {num}.",
    "The Borrower shall not permit the {name} to fall below {num} at any time.",
    "The {name} shall be maintained at a minimum of {num}, tested on each Quarter Date.",
    "The Borrower shall maintain a minimum {name} of {num}.",
    "7.12 {name}. The Borrower shall maintain, as of the end of each Fiscal Quarter, a {name} of {num} or more.",
    "The Borrower shall not: (a) create any Lien on its assets; or (b) permit the {name} as of the last day of any "
    "fiscal quarter to be less than {num}.",
    "The {name} shall not be lower than {num}, tested quarterly on a rolling basis.",
    "The Borrower covenants that its {name} shall be equal to or greater than {num} at all times.",
    "The Borrower shall ensure that the {name} shall be minimum {num} during the tenor of the Facility.",
    "Financial Covenants\nCovenant Requirement\n{name} Minimum {num}\nTested annually on audited accounts.",
    "6.3 Financial Covenants. The Borrower shall comply with the following: (i) {name}: not less than {num}; and "
    "(ii) the Borrower shall deliver a compliance certificate.",
    # Bug hunt 3 (9 November 2026): a term sheet's label form.
    "Financial Covenants: Minimum {name}: {num}; Maximum Capital Expenditure: as agreed with the Lender.",
]
CEILING_FORMS = [
    "The Borrower shall maintain a {name} of not more than {num}.",
    "The Borrower shall ensure that the {name} does not at any time exceed {num}.",
    "The Borrower will not permit the {name} as of the last day of any Fiscal Quarter to exceed {num}.",
    "The Borrower shall not permit the {name} to be greater than {num} at any time.",
    "The {name} shall not exceed {num} at any point of time during the tenor of the loan.",
    "The Borrower shall maintain a maximum {name} of {num}.",
    "7.11 {name}. The Borrower shall not permit the {name} as of the end of any Fiscal Quarter to be more than {num}.",
    "The Borrower shall not: (a) create any Lien on its assets; or (b) permit the {name} as of the last day of any "
    "fiscal quarter to exceed {num}.",
    "The {name} shall be equal to or less than {num}, tested quarterly.",
    "The Borrower shall keep its {name} at or below {num}.",
    "Financial Covenants\nCovenant Requirement\n{name} Maximum {num}\nTested annually on audited accounts.",
    "6.3 Financial Covenants. The Borrower shall comply with the following: (i) {name}: not more than {num}; and "
    "(ii) the Borrower shall deliver a compliance certificate.",
    "The Borrower will not permit the {name} as of the last day of any Fiscal Quarter to exceed the ratio set forth "
    "opposite such Fiscal Quarter:\nFiscal Quarter Ending Maximum {name}\nMarch 31, 2026 {num}\n"
    "June 30, 2026 2.00 to 1.00",
    "Financial Covenants: Maximum {name}: {num}; Minimum Capital Expenditure: as agreed with the Lender.",
]
# A threshold given by a defined word.
DEFINED_FORMS = {
    "min": ("“{name} Minimum” means {num}.", "If the {name} on any Test Date is less than the {name} Minimum, "
            "an Event of Default shall occur."),
    "max": ("“Maximum {name}” means {num}.", "The Borrower shall not permit the {name} on any Test Date to "
            "exceed the Maximum {name}."),
}
DISTRACTORS = [
    "“Applicable Margin” means, for any day, the rate per annum set forth below opposite the Leverage Ratio: "
    "Level I Leverage Ratio less than 1.50 to 1.00 1.25%; Level II Leverage Ratio greater than or equal to "
    "1.50 to 1.00 1.75%.",
    "The Borrower may incur Indebtedness so long as, after giving effect thereto on a pro forma basis, the Leverage "
    "Ratio would not exceed 3.00 to 1.00.",
    "Investments not to exceed in the aggregate the greater of $25,000,000 and 30% of Consolidated EBITDA.",
    "If the Leverage Ratio as of the last day of any fiscal year is greater than 3.00 to 1.00, the Borrower shall "
    "prepay the Loans in an amount equal to 50% of Excess Cash Flow.",
    "“Leverage Ratio” means, as of any date, the ratio of Total Debt to Consolidated EBITDA for the period of "
    "four fiscal quarters then ended.",
    "The Borrower shall deliver, within 45 days after the end of each fiscal quarter, a Compliance Certificate setting "
    "forth the Leverage Ratio and the Interest Coverage Ratio.",
    "As of the Closing Date, the Leverage Ratio of the Borrower is 2.10 to 1.00 and the Interest Coverage Ratio is "
    "4.50 to 1.00.",
    "As a condition to the extension option, the Debt Yield shall be at least 9% as reasonably determined by the "
    "Lender, as evidence satisfactory to the Lender.",
    "The Borrower shall pay a commitment fee of 0.35% per annum on the unused Commitments, and interest at 2.25% "
    "above the Base Rate.",
    "The Borrower may prepay the Loans in whole or in part upon not less than 3 Business Days notice, in a minimum "
    "amount of $1,000,000.",
    # Minimum EBITDA read outside an "EBITDA" clause (9 November 2026): the word is in pricing grids and definitions.
    "Applicable Margin: Level I Consolidated EBITDA greater than $100,000,000 2.00%; Level II Consolidated EBITDA "
    "equal to or less than $100,000,000 2.50%.",
    "“Consolidated EBITDA” means net income plus non-cash charges, provided that add-backs shall not be less "
    "than $1,000,000.",
    "If Consolidated EBITDA for any period is less than $10,000,000, the Borrower may cure the shortfall with an "
    "equity contribution.",
]
FILLER_BEFORE = ("ARTICLE VII COVENANTS\nSo long as any Loan remains outstanding, the Borrower agrees with the Lenders "
                 "as follows.\nThe Borrower shall keep proper books of record and account in accordance with GAAP.\n")
FILLER_AFTER = ("\nThe Borrower shall maintain insurance with financially sound and reputable insurers.\nThe Borrower "
                "shall not change its fiscal year without the consent of the Required Lenders.\n")


@dataclass
class Case:
    family: str | None
    direction: str
    value: float | None  # in currency units for amounts
    text: str
    label: str


def _amount_value(t) -> float | None:
    if t.threshold is None:
        return None
    unit = (t.unit or "").lower()
    mult = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "million": 1e6, "billion": 1e9}
    return t.threshold * mult.get(unit, 1.0)


def covenant_cases() -> list[Case]:
    cases = []
    for family, (direction, kind, names) in COVENANTS.items():
        forms = FLOOR_FORMS if direction == "min" else CEILING_FORMS
        if kind == "ratio":
            numbers = [(v, f) for v in RATIO_VALUES[direction] for f in ratio_formats(v)]
            if family is None:
                numbers += [(150.0, "150%"), (9.0, "9.0%")]
        elif kind == "percent":
            numbers = [(v, f) for v in PERCENT_VALUES for f in percent_formats(v)]
        else:
            numbers = AMOUNTS
        for name, form, (value, num) in itertools.product(names, forms, numbers):
            cases.append(Case(family, direction, value, form.format(name=name, num=num),
                              f"{name} | {form[:40]} | {num}"))
        for name, (value, num) in itertools.product(names[:2], numbers[:3]):
            d, use = DEFINED_FORMS[direction]
            cases.append(Case(family, direction, value, d.format(name=name, num=num) + "\n" + use.format(name=name),
                              f"{name} | defined | {num}"))
    return cases


def _page(text: str) -> list[Page]:
    return [Page(5, FILLER_BEFORE + text + FILLER_AFTER, "TEXT", 1.0)]


def _flags(t) -> list[str]:
    p = SimpleNamespace(metric_raw=t.metric, operator=t.operator)
    return list(t.issues) + check_direction(p) + (["LOW_CONFIDENCE"] if t.confidence < 0.8 else [])


def outcome(case: Case) -> str:
    pages = _page(case.text)
    terms = extract_terms(pages)
    candidates, _ = possible_covenants(pages, terms)
    fin = [t for t in terms if t.category == "FINANCIAL" and t.threshold is not None]

    def right(t) -> bool:
        if case.family is None or FAMILY.get(t.metric) != case.family:
            return False
        if ("min" if (t.operator or "").startswith(">") else "max") != case.direction:
            return False
        got = _amount_value(t) if COVENANTS[case.family][1] == "amount" else t.threshold
        return got is not None and abs(got - case.value) <= max(0.011, 0.005 * abs(case.value))

    if any(right(t) for t in fin):
        return "found"
    if candidates or any(_flags(t) for t in fin):
        return "shown"
    return "silent_wrong" if fin else "missed"


def distractor_outcome(text: str) -> str:
    pages = _page(text)
    fin = [t for t in extract_terms(pages) if t.category == "FINANCIAL" and t.threshold is not None]
    return "clean_wrong" if any(not _flags(t) for t in fin) else "ok"


def report(show: int = 40) -> dict:
    counts: dict[str, int] = {}
    bad: list[tuple[str, str]] = []
    by_family: dict[str, dict[str, int]] = {}
    for case in covenant_cases():
        o = outcome(case)
        counts[o] = counts.get(o, 0) + 1
        fam = by_family.setdefault(str(case.family), {})
        fam[o] = fam.get(o, 0) + 1
        if o in ("missed", "silent_wrong"):
            bad.append((o, case.label))
    for text in DISTRACTORS:
        o = distractor_outcome(text)
        counts["distractor_" + o] = counts.get("distractor_" + o, 0) + 1
        if o != "ok":
            bad.append((o, text[:90]))
    return {"counts": counts, "by_family": by_family, "bad": bad}


if __name__ == "__main__":
    r = report()
    print("cases", sum(v for k, v in r["counts"].items() if not k.startswith("distractor")), r["counts"])
    for fam, c in r["by_family"].items():
        print(f"  {fam:10} {c}")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    groups: dict[str, int] = {}
    for o, label in r["bad"]:
        key = o + " | " + re.sub(r"\| [^|]*$", "", label)
        groups[key] = groups.get(key, 0) + 1
    for k, n in sorted(groups.items(), key=lambda kv: -kv[1])[:limit]:
        print(f"{n:4}  {k}")
