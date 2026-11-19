"""Flow A Normalize step: extracted terms → Core proposals (04-api-contracts.md §6.1).

A term maps to a Core library covenant by name; the expression is written in Core's DSL against the
master's own measure. A financial term with no library match becomes a custom candidate whose rule
uses DPS statement measures (``DPS_`` codes, §6.2). Non-financial terms map to MANUAL masters by name
or become MANUAL custom candidates.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

from dps.extraction.agreement import Term
from dps.integrations.core import CoreMaster

# metric → (Core master names to try, Core measure used when the master does not name exactly one)
LIBRARY: dict[str, tuple[tuple[str, ...], str | None]] = {
    "DSCR": (("Minimum DSCR",), "DSCR"),
    "ICR": (("Minimum Interest Coverage",), "INTEREST_COVER"),
    "NET_LEVERAGE_RATIO": (("Maximum Net Leverage",), "NET_LEVERAGE"),
    "MIN_TANGIBLE_NET_WORTH": (("Minimum Tangible Net Worth",), "TANGIBLE_NET_WORTH"),
    "CURRENT_RATIO": (("Current Ratio Floor",), "CURRENT_RATIO"),
    "QUICK_RATIO": (("Quick Ratio Floor",), "QUICK_RATIO"),
    "DEBT_TO_EQUITY": (("Debt to Equity Ceiling",), "DEBT_EQUITY"),
    "DEBT_TO_EBITDA": (("Total Debt to EBITDA Ceiling",), "DEBT_EBITDA"),
    "DEBT_TO_NET_WORTH": (("Debt to Net Worth Ceiling",), None),  # Core V14, DPS measures
    "EBITDA_TO_EMI": (("EBITDA to EMI Floor",), None),  # Core V14, DPS measures
    "INSURANCE_MAINTENANCE": (("Insurance Cover Maintained",), None),
    "CHANGE_OF_CONTROL": (("No Change of Control",), None),
}

# A DPS formula is a standard template, not the agreement's own definition of the metric (EBITDA add-backs, gross or
# net interest): a suggestion a person confirms, never applied on its own (full review 19 Nov 2026, F5).
TEMPLATE_ISSUE = ("FORMULA_TEMPLATE: DPS's standard formula for this metric, not the agreement's definition; confirm "
                  "it against the defined terms")

# Custom-candidate rules over DPS statement measures, for metrics the library does not hold yet.
# 12-month covenants (10 November 2026): ratios of a year's earnings use the trailing twelve months (<KEY>_TTM), so a
# quarterly statement is not tested on one quarter's EBITDA (Debt / EBITDA about four times too high). Without the
# twelve-month figure the covenant is "could not be checked", never a false breach. Balances stay as they are.
DPS_FORMULAS: dict[str, str] = {
    "DSCR": "DPS_EBITDA_TTM / (DPS_PRINCIPAL_PAID_TTM + DPS_INTEREST_PAID_TTM)",
    "ICR": "DPS_EBITDA_TTM / DPS_INTEREST_PAID_TTM",
    "DEBT_TO_EBITDA": "DPS_TOTAL_DEBT / DPS_EBITDA_TTM",
    "NET_LEVERAGE_RATIO": "DPS_NET_DEBT / DPS_EBITDA_TTM",
    "CURRENT_RATIO": "DPS_CURRENT_ASSETS / DPS_CURRENT_LIABILITIES",
    "QUICK_RATIO": "(DPS_CURRENT_ASSETS - DPS_INVENTORY) / DPS_CURRENT_LIABILITIES",
    "DEBT_TO_EQUITY": "DPS_TOTAL_DEBT / DPS_NET_WORTH",
    "MIN_TANGIBLE_NET_WORTH": "DPS_TANGIBLE_NET_WORTH",
    "DEBT_TO_NET_WORTH": "DPS_TOTAL_DEBT / DPS_NET_WORTH",
    "EBITDA_TO_EMI": "DPS_EBITDA_TTM / DPS_EMI_AMOUNT_TTM",
    "MIN_NET_WORTH": "DPS_NET_WORTH",
    "FIXED_CHARGE_COVERAGE_RATIO": "DPS_EBITDA_TTM / (DPS_INTEREST_PAID_TTM + DPS_PRINCIPAL_PAID_TTM)",
    "MINIMUM_LIQUIDITY": "DPS_CASH",
    "MIN_EBITDA": "DPS_EBITDA_TTM",
}

DISPLAY_NAMES: dict[str, str] = {
    "DEBT_TO_NET_WORTH": "Debt to Net Worth Ceiling",
    "EBITDA_TO_EMI": "EBITDA to EMI Floor",
    "MIN_NET_WORTH": "Minimum Net Worth",
    "NEGATIVE_PLEDGE": "Negative Pledge",
    "REPORTING_COVENANT": "Financial Reporting",
    "COMPLIANCE_CERTIFICATE": "Compliance Certificate",
    "BOOKS_AND_RECORDS": "Books and Records",
    "REGULATORY_AND_TAX_COMPLIANCE": "Regulatory and Tax Compliance",
    "SECURITY_COVER": "Minimum Security Cover",
    "MIN_COLLATERAL_VALUE": "Minimum Collateral Value",
    "ASSET_DISPOSAL_CAP": "Asset Disposal Limit",
    "LOAN_TO_VALUE": "Maximum Loan to Value",
    "MIN_EBITDA": "Minimum EBITDA",
}

# Amount metrics are held in INR crore (the measure unit); convert the agreement's unit.
TO_CRORE = {None: 1.0, "crore": 1.0, "crores": 1.0, "cr": 1.0, "lakh": 0.01, "lakhs": 0.01, "lac": 0.01,
            "lacs": 0.01, "million": 0.1, "billion": 100.0, "mn": 0.1, "bn": 100.0}
# Bug hunt (8 November 2026): "a Tangible Net Worth of not less than LKR 1,000,000,000" was published as 1,000,000,000
# INR crore. Only dollars were recognised; any other currency named in the quote now holds the amount for a person.
_CURRENCIES = [
    ("USD", re.compile(r"\$|\bUSD\b|\bdollars?\b", re.I)),
    ("LKR", re.compile(r"\bLKR\b|\bSLR\b|Sri\s+Lanka(?:n)?\s+Rupees?", re.I)),
    ("EUR", re.compile(r"\u20ac|\bEUR\b|\beuros?\b", re.I)),
    ("GBP", re.compile(r"\u00a3|\bGBP\b|\bsterling\b", re.I)),
    ("SGD", re.compile(r"\bSGD\b|\bS\$", re.I)),
    ("AED", re.compile(r"\bAED\b|\bdirhams?\b", re.I)),
]


def foreign_currency(quote: str | None) -> str | None:
    """The currency other than the rupee that an amount covenant's quote names, or None."""
    return next((code for code, rx in _CURRENCIES if rx.search(quote or "")), None)


def named_currency(quote: str | None) -> str | None:
    """Money for any country: the currency an amount covenant's quote names (INR included), or None ("Rs." alone)."""
    from dps.money import detect_currency

    return detect_currency(quote) or foreign_currency(quote)


@dataclass
class Proposal:
    metric_raw: str
    category: str
    covenant_master_id: str | None
    name: str
    expression: str | None
    assessment_type: str  # AUTO | MANUAL
    operator: str | None
    threshold: float | None
    unit: str | None
    frequency: str | None
    page: int | None
    quote: str
    definition: str | None
    confidence: float
    method: str = "REGEX"
    issues: list[str] = field(default_factory=list)
    threshold_raw: float | None = None  # as written in the agreement, before unit conversion
    # Phase 3b-1: {"levels": [{"threshold", "from", "to", "expression"}], "testMonths", "cron", "cure"} or None
    schedule: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _fmt(value: float) -> str:
    return f"{value:g}" if abs(value) < 1e15 else str(value)


INFO_NAMES: dict[str, str] = {
    "AGREEMENT_DATE": "Agreement date", "EFFECTIVE_DATE": "Effective date", "MATURITY_DATE": "Maturity date",
    "FIRST_TEST_DATE": "First test date", "CURE_PERIOD_DAYS": "Cure period", "TESTING_BASIS": "Testing basis",
    "STEP_SCHEDULE": "Step-down schedule",
}


def _info_proposal(term: Term) -> Proposal:
    """FR-29/FR-30: review information, never a covenant; the value travels in ``expression``."""
    value = term.definition
    if value is None and term.threshold is not None:
        value = f"{_fmt(term.threshold)} {(term.unit or '').lower()}".strip()
    return Proposal(metric_raw=term.metric, category="INFO", covenant_master_id=None,
                    name=INFO_NAMES.get(term.metric, term.metric.title()), expression=value, assessment_type="MANUAL",
                    operator=None, threshold=term.threshold, unit=term.unit, frequency=None, page=term.page,
                    quote=term.quote, definition=None, confidence=term.confidence, method=term.method,
                    issues=list(term.issues), threshold_raw=term.threshold)


def _candidate_proposal(term: Term) -> Proposal:
    """A covenant-shaped sentence DPS did not understand: a person decides what it is (coverage.py)."""
    # Stable across runs (unlike hash()), so a candidate a person already decided keeps its identity on reprocessing.
    digest = hashlib.sha256(term.quote.encode()).hexdigest()[:10]
    return Proposal(metric_raw=f"POSSIBLE_COVENANT:{term.page}:{digest}", category="CANDIDATE",
                    covenant_master_id=None, name=term.name or "Possible covenant", expression=None,
                    assessment_type="MANUAL", operator=None, threshold=None, unit=None, frequency=None, page=term.page,
                    quote=term.quote, definition=None, confidence=term.confidence, method=term.method,
                    issues=list(term.issues))


def to_proposal(term: Term, masters_by_name: dict[str, CoreMaster], currency: str | None = "INR") -> Proposal:
    """``currency`` is the loan's: an amount limit is kept in its money unit (INR crore, millions of any other)."""
    if term.category == "INFO":
        return _info_proposal(term)
    if term.category == "CANDIDATE":
        return _candidate_proposal(term)
    names, measure = LIBRARY.get(term.metric, ((), None))
    master = next((masters_by_name[_key(n)] for n in names if _key(n) in masters_by_name), None)

    threshold, unit = term.threshold, term.unit
    issues = list(term.issues)
    amount = term.metric in ("MIN_NET_WORTH", "MIN_TANGIBLE_NET_WORTH", "MINIMUM_LIQUIDITY", "MIN_COLLATERAL_VALUE",
                             "ASSET_DISPOSAL_CAP", "MIN_EBITDA")
    named = ("USD" if unit == "usd" else named_currency(term.quote)) if amount else None
    other = named if named and named != currency else None
    if amount and threshold is not None and currency is None:
        # Phase 2: nobody knows the loan's currency (the CP service did not answer): the limit is kept as written
        # and waits for a person.
        other = named or "UNKNOWN"
        unit = named or unit
        issues.append("LOAN_CURRENCY_UNKNOWN: the loan's currency is not known; set it in the borrower's settings, "
                      "then check this limit")
    elif amount and threshold is not None and other:
        # The statement figures are in the loan's currency: a limit in another cannot be compared until a person
        # converts it (money for any country, 10 November 2026; was AMOUNT_NOT_IN_INR).
        unit = other
        issues.append(f"AMOUNT_CURRENCY_DIFFERS: the limit is in {other}, the loan in {currency}; convert it to "
                      f"{_money_words(currency)} before applying")
    elif amount and threshold is not None and currency == "INR":
        threshold = round(threshold * TO_CRORE.get(unit, 1.0), 6)
        unit = "INR_CR"
    elif amount and threshold is not None:
        from dps.money import SCALE_UNITS, money_unit, to_money_unit

        scale = unit if unit in SCALE_UNITS else None
        if scale is None and threshold < 100_000:
            # "LKR 500" with no scale word: 500 rupees is no covenant, 500 million or 500 thousand is a guess.
            issues.append(f"AMOUNT_SCALE_UNCLEAR: no scale is stated for {threshold:g}; read as {currency} million, "
                          "check the agreement")
            scale = "million"
        threshold = round(to_money_unit(threshold, scale, currency), 6)
        unit = money_unit(currency)
    if term.metric == "LOAN_TO_VALUE":
        unit = "PERCENT"  # loan / collateral value: no statement measure, a person enters it each period

    base = dict(metric_raw=term.metric, category=term.category, operator=term.operator, threshold=threshold,
                unit=unit, frequency=term.frequency, page=term.page, quote=term.quote,
                definition=term.definition, confidence=term.confidence, method=term.method,
                issues=issues, threshold_raw=term.threshold)
    rule = f"{term.operator} {_fmt(threshold)}" if term.operator and threshold is not None else None
    if amount and other:
        rule = None  # held as a manual covenant until the amount is in the loan's currency

    if master is not None:
        manual = master.assessment_type == "MANUAL"
        lhs = master.referenced_measures[0] if len(master.referenced_measures) == 1 else measure
        if lhs is None and DPS_FORMULAS.get(term.metric):
            lhs = DPS_FORMULAS[term.metric]
            issues.append(TEMPLATE_ISSUE)
        expression = None if manual or rule is None or lhs is None else f"{lhs} {rule}"
        return Proposal(covenant_master_id=master.id, name=master.name, expression=expression,
                        assessment_type="MANUAL" if manual else "AUTO",
                        schedule=_schedule(term, lhs if expression else None), **base)

    lhs = DPS_FORMULAS.get(term.metric)
    expression = f"{lhs} {rule}" if lhs and rule else None
    if expression:
        issues.append(TEMPLATE_ISSUE)
    name = DISPLAY_NAMES.get(term.metric, term.metric.replace("_", " ").title())
    return Proposal(covenant_master_id=None, name=name, expression=expression,
                    assessment_type="AUTO" if expression else "MANUAL",
                    schedule=_schedule(term, lhs if expression else None), **base)


def _schedule(term: Term, lhs: str | None) -> dict | None:
    """Phase 3b-1: the step-down levels, each with its own rule, the test months and Core's schedule for them, and
    the cure right. None when the agreement gives none of them."""
    from dps.extraction.schedule import cron_for

    levels = [{**lv, "expression": f"{lhs} {term.operator} {_fmt(lv['threshold'])}" if lhs and term.operator
               else None} for lv in term.levels or []]
    out = {"levels": levels, "testMonths": term.test_months or [], "cron": cron_for(term.test_months or []),
           "cure": term.cure}
    return out if levels or term.test_months or term.cure else None


def base_metric(metric_raw: str) -> str:
    """"DEBT_TO_EBITDA:SENIOR_SECURED_LEVERAGE_RATIO" -> "DEBT_TO_EBITDA" (the family of a second covenant)."""
    return metric_raw.split(":", 1)[0]


def _money_words(currency: str) -> str:
    return "INR crore" if currency == "INR" else f"{currency} million"


def normalize(terms: list[Term], masters: list[CoreMaster], currency: str | None = "INR") -> list[Proposal]:
    by_name = {_key(m.name): m for m in masters}
    out, seen = [], set()
    for t in terms:
        p = to_proposal(t, by_name, currency)
        if t.category == "FINANCIAL" and t.metric in seen and t.name:
            # A second covenant of one family (senior secured leverage beside total leverage) cannot use the same
            # library covenant on a CP: it becomes a custom covenant under its own name, and a person checks
            # its formula, which is the family's (for example total debt, not senior debt, over EBITDA).
            # dps_extracted_term.metric_raw holds 128 characters; a long name (the AI reader's) must not fail the job.
            p.metric_raw = f"{t.metric}:{re.sub(r'[^A-Z0-9]+', '_', t.name.upper()).strip('_')}"[:128]
            p.covenant_master_id = None
            lhs = DPS_FORMULAS.get(t.metric)
            p.expression = f"{lhs} {p.operator} {_fmt(p.threshold)}" if lhs and p.expression else None
            p.assessment_type = "AUTO" if p.expression else "MANUAL"
            p.name = t.name.title() if t.name.islower() else t.name
            p.issues.append("SECOND_COVENANT_OF_FAMILY: check the formula measures this covenant")
        seen.add(t.metric)
        out.append(p)
    return out
