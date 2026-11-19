"""Flow A Extract step: deterministic covenant extraction from loan agreement pages.

Ported from covenanttrackingphase1.2 ``LoanAgreementExtractionTool`` (numbered-clause scan, metric
aliases, threshold phrases, net-worth amounts, non-financial rules), reworked to be page-aware: every
term carries the page it was found on and a verbatim quote, so the Validate step can check citations.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from dps.parse.engine import Page

# Our metric keys → phrases (metric_mappings.json, trimmed to the reliable multi-word forms).
FINANCIAL_ALIASES: dict[str, list[str]] = {
    "DSCR": ["DSCR", "DEBT SERVICE COVERAGE RATIO", "DEBT SERVICE COVERAGE"],
    "ICR": ["ICR", "INTEREST COVERAGE RATIO", "INTEREST COVERAGE", "INTEREST COVER"],
    "DEBT_TO_EBITDA": ["DEBT TO EBITDA", "TOTAL DEBT TO EBITDA", "DEBT/EBITDA", "TOTAL LEVERAGE RATIO"],
    "NET_LEVERAGE_RATIO": ["NET LEVERAGE RATIO", "NET DEBT TO EBITDA", "NET DEBT/EBITDA"],
    "DEBT_TO_NET_WORTH": ["DEBT TO NET WORTH", "TOTAL DEBT TO NET WORTH", "DEBT/NET WORTH"],
    "DEBT_TO_EQUITY": ["DEBT TO EQUITY", "DEBT-TO-EQUITY", "DEBT/EQUITY", "TOTAL DEBT TO EQUITY"],
    "EBITDA_TO_EMI": ["EBITDA TO EMI", "EBITDA/EMI", "EMI COVERAGE"],
    "CURRENT_RATIO": ["CURRENT RATIO", "WORKING CAPITAL RATIO"],
    "QUICK_RATIO": ["QUICK RATIO", "ACID TEST RATIO"],
    "FIXED_CHARGE_COVERAGE_RATIO": ["FIXED CHARGE COVERAGE RATIO", "FIXED CHARGE COVERAGE", "FCCR"],
    "MIN_TANGIBLE_NET_WORTH": ["MINIMUM TANGIBLE NET WORTH", "TANGIBLE NET WORTH"],
    "MIN_NET_WORTH": ["MINIMUM NET WORTH", "NET WORTH"],
    "MINIMUM_LIQUIDITY": ["MINIMUM LIQUIDITY", "MINIMUM CASH BALANCE", "MINIMUM UNRESTRICTED CASH"],
    "LOAN_TO_VALUE": ["LOAN TO VALUE RATIO", "LOAN-TO-VALUE RATIO", "LOAN TO VALUE", "LOAN-TO-VALUE", "LTV RATIO",
                      "LTV"],
    "MIN_EBITDA": ["MINIMUM EBITDA", "MINIMUM CONSOLIDATED EBITDA"],
}
# Names used in US syndicated and bilateral credit agreements (benchmark of 50 real agreements, 6 November 2026).
for _metric, _names in {
    "DEBT_TO_EBITDA": ["CONSOLIDATED LEVERAGE RATIO", "CONSOLIDATED TOTAL LEVERAGE RATIO", "LEVERAGE RATIO",
                       "FUNDED DEBT TO EBITDA", "TOTAL FUNDED DEBT TO EBITDA", "SENIOR FUNDED DEBT TO EBITDA",
                       "SENIOR DEBT TO EBITDA", "CASH FLOW LEVERAGE RATIO", "SENIOR SECURED LEVERAGE RATIO",
                       "CONSOLIDATED SENIOR SECURED LEVERAGE RATIO", "MAXIMUM TOTAL LEVERAGE RATIO"],
    "NET_LEVERAGE_RATIO": ["TOTAL NET LEVERAGE RATIO", "FIRST LIEN NET LEVERAGE RATIO", "SECURED NET LEVERAGE RATIO",
                           "CONSOLIDATED NET SENIOR SECURED LEVERAGE RATIO", "SENIOR SECURED NET LEVERAGE RATIO"],
    "ICR": ["CONSOLIDATED INTEREST COVERAGE RATIO"],
    "FIXED_CHARGE_COVERAGE_RATIO": ["CONSOLIDATED FIXED CHARGE COVERAGE RATIO", "BASIC FIXED CHARGE COVERAGE RATIO"],
    "DSCR": ["GLOBAL DEBT SERVICE COVERAGE RATIO", "NET DEBT SERVICE COVERAGE RATIO", "CASH FLOW COVERAGE RATIO",
             "CASH FLOW COVERAGE", "CASH FLOW TO DEBT SERVICE"],
    "DEBT_TO_NET_WORTH": ["DEBT TO TANGIBLE NET WORTH", "INDEBTEDNESS TO TANGIBLE NET WORTH",
                          "FUNDED DEBT TO TANGIBLE NET WORTH", "DEBT-TO-NET WORTH RATIO"],
    "DEBT_TO_EQUITY": ["NET DEBT TO EQUITY RATIO", "NET DEBT TO EQUITY"],
}.items():
    FINANCIAL_ALIASES[_metric] = FINANCIAL_ALIASES[_metric] + [n for n in _names if n not in FINANCIAL_ALIASES[_metric]]
# Names in Indian, UK and Sri Lankan facility agreements (wording stress test, 8 November 2026).
for _metric, _names in {
    "DSCR": ["DEBT SERVICE COVER RATIO", "DEBT SERVICE COVER"],
    "ICR": ["INTEREST SERVICE COVERAGE RATIO", "ISCR"],
    "DEBT_TO_NET_WORTH": ["TOL/TNW", "TOL TO TNW", "TOTAL OUTSIDE LIABILITIES TO TANGIBLE NET WORTH",
                          "TOTAL OUTSIDE LIABILITIES/TANGIBLE NET WORTH"],
    "DEBT_TO_EQUITY": ["GEARING RATIO", "GEARING"],
    "MINIMUM_LIQUIDITY": ["UNRESTRICTED CASH", "LIQUID ASSETS", "LIQUIDITY"],
}.items():
    FINANCIAL_ALIASES[_metric] = FINANCIAL_ALIASES[_metric] + [n for n in _names if n not in FINANCIAL_ALIASES[_metric]]
# Sri Lanka run (10 November 2026): facility letters of Sri Lankan banks.
for _metric, _names in {
    "DEBT_TO_NET_WORTH": ["TOTAL LIABILITIES TO TANGIBLE NET WORTH", "TOTAL LIABILITIES TO NET WORTH",
                          "TOTAL LIABILITIES/TANGIBLE NET WORTH"],
    "ICR": ["EBITDA TO INTEREST EXPENSE", "EBITDA TO INTEREST", "EBITDA/INTEREST"],
    "DEBT_TO_EQUITY": ["DEBT EQUITY RATIO", "TOTAL BORROWINGS TO EQUITY", "TOTAL BORROWINGS TO TOTAL EQUITY"],
}.items():
    FINANCIAL_ALIASES[_metric] = FINANCIAL_ALIASES[_metric] + [n for n in _names if n not in FINANCIAL_ALIASES[_metric]]

# Phase 4 learning loop: covenant names an administrator added from reviewers' decisions (set per document by
# Flow A from dps_learned_wording). Kept apart from the built-in names so retiring one takes effect at once.
_LEARNED_COVENANTS: dict[str, list[str]] = {}


def set_learned_covenants(names: dict[str, list[str]]) -> None:
    _LEARNED_COVENANTS.clear()
    _LEARNED_COVENANTS.update({m: [n.upper() for n in ns] for m, ns in names.items()})


def _aliases() -> dict[str, list[str]]:
    if not _LEARNED_COVENANTS:
        return FINANCIAL_ALIASES
    merged = {m: list(p) for m, p in FINANCIAL_ALIASES.items()}
    for m, names in _LEARNED_COVENANTS.items():
        merged.setdefault(m, [])
        merged[m] += [n for n in names if n not in merged[m]]
    return merged


AMOUNT_METRICS = {"MIN_NET_WORTH", "MIN_TANGIBLE_NET_WORTH", "MINIMUM_LIQUIDITY", "MIN_EBITDA"}
# Thresholds written as a percentage ("75.00% or less", "sixty five percent (65%)").
PERCENT_METRICS = {"LOAN_TO_VALUE"}
# Ratios a borrower must stay above; "maintain a DSCR ... of 1.25 to 1.0" with no comparison word is a floor.
FLOOR_METRICS = {"DSCR", "ICR", "EBITDA_TO_EMI", "CURRENT_RATIO", "QUICK_RATIO", "FIXED_CHARGE_COVERAGE_RATIO",
                 "MIN_TANGIBLE_NET_WORTH", "MIN_NET_WORTH", "MINIMUM_LIQUIDITY", "MIN_EBITDA"}
# One agreement can hold several covenants of one family (total and senior secured leverage, gross and net DSCR).
MAX_PER_METRIC = 3
# A ratio covenant above this is a misread number ("90 days"), not a threshold.
MAX_RATIO = 25.0
# Text around a ratio that makes it something other than a maintenance covenant: a pricing grid, an
# incurrence test for new debt or payments, a cash sweep, or a condition for releasing funds or extending.
_NOT_A_COVENANT = re.compile(
    r"pro\s+forma|after\s+giving\s+effect|applicable\s+(rate|margin|percentage)|pricing\s+level|asset\s+sale\s+"
    r"percentage|ecf\s+percentage|excess\s+cash\s+flow|incremental|until\s+such\s+time|evidence\s+satisfactory|"
    r"extension\s+(option|period)|extended\s+maturity|release\s+of\s+(the\s+)?(funds|letter)", re.I)

_INTENT = re.compile(r"\b(shall|must|required\s+to|undertakes?\s+to|agree(?:s|d)?\s+to|will\s+not)\b", re.I)

NON_FINANCIAL_RULES: list[tuple[str, list[str], list[str]]] = [
    # (metric, body patterns, heading-only patterns)
    ("COMPLIANCE_CERTIFICATE",
     [r"\bcompliance\s+certificate\b", r"\bofficer\s+certificate\b", r"\bcertificate\s+of\s+compliance\b",
      r"\bcertificate\s+signed\s+by\b",
      r"\bcertif\w*\b[^.;]{0,80}\bchief\s+financial\s+officer\b|\bchief\s+financial\s+officer\b[^.;]{0,80}\bcertif"],
     [r"\bcompliance\s+certificate\b", r"\bofficer\s+certificate\b", r"\bcertificate\s+of\s+compliance\b"]),
    ("REPORTING_COVENANT",
     [r"\binformation\s+covenant\b", r"\bfinancial\s+statements?\s+and\s+information\b",
      r"\bsubmit\s+(?:audited\s+)?financial\s+statements?\b",
      r"\bdeliver\s+(?:monthly|quarterly|annual|yearly)\s+(?:financial\s+)?reports?\b",
      r"\bprovide\s+(?:monthly|quarterly|annual|yearly)\s+(?:financial\s+)?statements?\b"],
     [r"\bfinancial\s+statements?\s+and\s+information\b", r"\bfinancial\s+information\b"]),
    ("NEGATIVE_PLEDGE",
     # "shall not, without the prior written consent of the Lender, create any lien" puts words in between.
     [r"\bnegative\s+pledge\b", r"\bshall\s+not\b[^.]{0,80}?\bcreate\s+(?:any\s+)?(?:lien|charge|encumbrance)\b",
      r"\bno\s+encumbrance\b", r"\bwithout\s+prior\s+written\s+consent\b"], []),
    ("DEBT_INCURRANCE_RESTRICTION",
     [r"\bshall\s+not\s+incur\s+additional\s+indebtedness\b", r"\bno\s+further\s+borrowing\b",
      r"\badditional\s+debt\s+requires\s+(?:prior\s+)?consent\b"], []),
    ("ASSET_DISPOSAL_RESTRICTION",
     [r"\bshall\s+not\s+dispose\s+of\s+assets\b", r"\bno\s+sale\s+or\s+transfer\s+of\s+(?:material\s+)?assets\b",
      r"\boutside\s+ordinary\s+course\s+of\s+business\b"], []),
    ("CHANGE_OF_CONTROL",
     [r"\bchange\s+of\s+control\b", r"\bpromoter\s+shareholding\b", r"\bmanagement\s+control\b"], []),
    ("INSURANCE_MAINTENANCE",
     [r"\bmaintain\s+adequate\s+insurance\b", r"\bkeep\s+assets\s+insured\b", r"\binsurance\s+policy\s+assignment\b",
      r"\binsurance\s+(?:shall\s+be\s+)?maintained\b", r"\bmaintain\s+.*\binsurance\b",
      r"\bkeep\s+.*\binsurance\b"], []),
    ("BOOKS_AND_RECORDS",
     [r"\bbooks?\s+and\s+records?\b", r"\bmaintain\s+proper\s+books?\s+of\s+account\b"],
     [r"\bbooks?\s+and\s+records?\b"]),
    ("REGULATORY_AND_TAX_COMPLIANCE",
     [r"\bcomply\s+with\s+all\s+applicable\s+laws\b", r"\btimely\s+payment\s+of\s+tax(?:es)?\b",
      r"\bstatutory\s+dues\b", r"\bmaintain\s+licen[cs]es?\s+and\s+permits?\b"], []),
]
# Non-financial wording stress test (8 November 2026): the usual US, UK and Indian wordings the rules above lacked.
_SHALL_NOT = r"\b(?:shall|will)\s+not\b[^.]{0,120}?"
for _metric, _more in {
    "REPORTING_COVENANT": [
        r"\b(?:deliver|furnish|submit|provide|send)\b[^.]{0,200}?\b(?:financial\s+statements?|balance\s+sheets?|"
        r"(?:annual|audited)\s+accounts|statements?\s+of\s+(?:income|operations|cash\s+flows?))\b",
        r"\bwithin\s+(?:\d+|[a-z]+)\s+(?:days|months)\s+(?:after|from|of)\s+the\s+(?:end|close)\s+of\s+each\s+"
        r"(?:fiscal|financial)\s+(?:year|quarter)\b"],
    "NEGATIVE_PLEDGE": [
        _SHALL_NOT + r"\b(?:create|incur|assume|grant|permit\s+to\s+(?:exist|subsist)|suffer\s+to\s+exist)\b[^.]{0,80}?"
        r"\b(?:liens?|charges?|encumbrances?|security\s+interests?|mortgages?|pledges?)\b",
        r"\b(?:shall|will)\s+not\s+(?:pledge|mortgage|hypothecate|encumber)\b"],
    "DEBT_INCURRANCE_RESTRICTION": [
        _SHALL_NOT + r"\b(?:incur|create|assume|suffer\s+to\s+exist)\b[^.]{0,60}?\bindebtedness\b",
        r"\bfurther\s+borrowings?\b", _SHALL_NOT + r"\bborrow\b"],
    "ASSET_DISPOSAL_RESTRICTION": [
        _SHALL_NOT + r"\b(?:sell|transfer|convey|lease|dispose)\b[^.]{0,120}?\b(?:assets?|property|properties)\b"],
    "CHANGE_OF_CONTROL": [
        r"\bchange\s+(?:in|of)\s+(?:its\s+)?(?:ownership|control|management)\b",
        r"\bpromoters?\b[^.]{0,80}?\b(?:hold|shareholding|stake)\b"],
    "INSURANCE_MAINTENANCE": [r"\b(?:keep|maintain)\b[^.]{0,60}?\binsured\b"],
    "BOOKS_AND_RECORDS": [r"\bbooks?\s+of\s+(?:record|account)s?\b"],
}.items():
    for _rule in NON_FINANCIAL_RULES:
        if _rule[0] == _metric:
            _rule[1].extend(_more)

_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_UNIT = r"\s*(crores?|cr\b|lakhs?|lacs?|million|billion|mn\b|bn\b)?"  # "LKR 8,000 Mn" (Sri Lanka run)
# "shall not (be|fall|go|drop) less than / below": a verb may sit between the negation and the comparison.
# Without it the bare "less than" rule below matches and the floor is read as a ceiling.
# Bug hunt (8 November 2026): "shall not at any time be lower than 1.2x" was missed.
_NOT = r"(?:not|never|at\s+no\s+time)\s+(?:at\s+any\s+time\s+)?(?:(?:be|fall|go|drop|rise)\s+)?"
# "shall not exceed (a) 4.00:1.00 for any Test Period ending on or before ... and (b) 3.50:1.00 thereafter": a list
# marker may stand before the first level (bug hunt 3).
# Money for any country (10 November 2026): "Sri Lankan Rupees 2,500 million", "PKR", "AED" and the like name the
# currency before an amount too (dps.money reads which one).
_MONEY = (r"(?:INR|Rs\.?|₹|US\$|\$|LKR|SLRs?|USD|PKR|NPR|BDT|AED|SAR|QAR|GBP|£|EUR|€|SGD|S\$"
          r"|(?:(?:Sri\s+Lankan?|Indian|Pakistani?|Nepalese|Nepal)\s+)?Rupees?)")
_CUR = r"(?:\((?:[a-h]|i{1,3}|iv|v)\)\s*)?" + _MONEY + "?"
# "will not permit the Interest Coverage Ratio ... to be less than 3.00": a floor, though it says "less than".
_PERMIT = r"(?:shall|will)\s+not\s+(?:permit|allow|suffer)\b[^.;]{0,220}?"
_WORD_THRESHOLDS: list[tuple[str, str]] = [
    # "shall not permit the Leverage Ratio to be greater than or equal to 3.50": below 3.50 (bug hunt).
    (_PERMIT + r"(?:to\s+)?(?:be\s+)?(?:greater|more|higher)\s+than\s+or\s+equal\s+to\s*" + _CUR + r"\s*" + _NUM
     + _UNIT, "<"),
    (_PERMIT + r"(?:to\s+)?(?:be\s+)?(?:less|lower)\s+than\s+or\s+equal\s+to\s*" + _CUR + r"\s*" + _NUM + _UNIT,
     ">"),
    (_PERMIT + r"(?:to\s+)?(?:be\s+|fall\s+)?(?:less|lower)\s+than\s*" + _CUR + r"\s*" + _NUM + _UNIT, ">="),
    (_PERMIT + r"(?:to\s+)?(?:fall|go|drop|decline)\s+below\s*" + _CUR + r"\s*" + _NUM + _UNIT, ">="),
    (_PERMIT + r"(?:to\s+)?(?:rise|go|increase)\s+(?:above|beyond)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<="),
    # Bug hunt (8 November 2026): "the Interest Coverage Ratio is at all times greater than or equal to 2.5x" and
    # "shall at all times be less than or equal to 3.5x" were missed by the rules and the safety net.
    (r"(?:(?:greater|more|higher)\s+than\s+or\s+equal\s+to|at\s+or\s+above)\s*" + _CUR + r"\s*" + _NUM + _UNIT,
     ">="),
    (r"(?:(?:less|lower)\s+than\s+or\s+equal\s+to|at\s+or\s+below)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<="),
    (_PERMIT + r"(?:to\s+)?(?:exceed|be\s+(?:greater|more|higher)\s+than)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<="),
    (r"(?:equals?\s+or\s+exceeds?|equal\s+to\s+or\s+greater\s+than|in\s+excess\s+of)\s*" + _CUR + r"\s*" + _NUM
     + _UNIT, ">="),
    (r"(?:never\s+exceed|shall\s+not\s+exceed|equal\s+to\s+or\s+less\s+than)\s*" + _CUR + r"\s*" + _NUM + _UNIT,
     "<="),
    (r"(?:" + _NOT + r"(?:less|lower)\s+than|" + _NOT + r"below|at\s+least|minimum(?:\s+of)?|no\s+less\s+than"
     r"|no\s+lower\s+than)\s*" + _CUR + r"\s*" + _NUM + _UNIT, ">="),
    (r"(?:does\s+not\s+(?:at\s+any\s+time\s+)?exceed|not\s+(?:to\s+)?exceed(?:ing)?|at\s+no\s+time\s+exceed|at\s+most"
     r"|maximum(?:\s+of)?|" + _NOT + r"(?:more|greater|higher)\s+than|" + _NOT + r"(?:above|beyond)"
     r"|no\s+more\s+than|no\s+greater\s+than)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<="),
    # Sri Lanka run: "Current ratio should be maintained above 1.5", "Debt/Equity Ratio shall be maintained below
    # 1.25:1", "maintained at 1.20 times or above".
    (r"(?:maintained|kept)\s+(?:at\s+)?(?:above|over)\s*" + _CUR + r"\s*" + _NUM + _UNIT, ">"),
    (r"(?:maintained|kept)\s+(?:at\s+)?(?:below|under)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<"),
    (r"\bat\s+" + _NUM + _UNIT + r"\s*(?:x\b|times\b|:\s*1(?:\.0+)?)?\s+or\s+(?:above|more|higher|over)\b", ">="),
    (r"\bat\s+" + _NUM + _UNIT + r"\s*(?:x\b|times\b|:\s*1(?:\.0+)?)?\s+or\s+(?:below|less|lower|under)\b", "<="),
    (r"(?:greater\s+than|more\s+than|(?:remain|stay)\s+above)\s*" + _CUR + r"\s*" + _NUM + _UNIT, ">"),
    # Bug hunt 3: "If Consolidated EBITDA ... is less than $10,000,000, the Borrower may cure": the "$" was not
    # read here, so the amount was taken for a floor.
    (r"(?:less\s+than)\s*" + _CUR + r"\s*" + _NUM + _UNIT, "<"),
]
_RATIO = _NUM + r"\s*(?:x\b|times\b|:\s*1(?:\.0+)?(?!\d)|to\s+1(?:\.0+)?(?!\d))"
# Step-down tables: "to exceed the ratio set forth opposite such period: ... June 30, 2025 4.75 to 1.00" and
# "EBITDA of at least the amounts indicated for each period specified below: ... September 30, 2017 $9,500,000".
# The first level of the table is taken; the rest of the schedule is in the quote for the reviewer.
_TABLE = r"\s+the\s+(?:ratios?|amounts?|levels?)\s+(?:set\s+forth|indicated|specified|shown)\b[^;]{0,300}?"
# A level is a ratio, an amount, or a bare number after the row's date ("March 31, 2026 3.50"; wording stress test:
# the second level was taken when the first had no "to 1.00").
_TABLE_LEVEL = r"(?:" + _RATIO + r"|\$\s*" + _NUM + r"|\b(?:19|20)\d\d\s+" + _NUM + r"(?![\d,]*\s*(?:%|per\s*cent)))"
_TABLE_THRESHOLDS: list[tuple[str, str]] = [
    (r"(?:at\s+least|not\s+(?:be\s+)?less\s+than|minimum\s+of)" + _TABLE + _TABLE_LEVEL, ">="),
    (r"(?:exceed|(?:be\s+)?(?:greater|more|higher)\s+than)" + _TABLE + _TABLE_LEVEL, "<="),
]
# "maintain a minimum Debt Service Coverage Ratio of 1.25:1", "a Maximum Leverage Ratio of 3.0x".
_RATIO_TAIL = r"\s*(?:x\b|times\b|:\s*1(?:\.0+)?\b|to\s+1(?:\.0+)?\b)?"
_NAMED_THRESHOLDS: list[tuple[str, str]] = [
    (r"\bminimum\s+(?:[\w\-\u201c\u201d\"]+\s+){1,7}?(?:of|at)\s+" + _RATIO, ">="),
    (r"\bmaximum\s+(?:[\w\-\u201c\u201d\"]+\s+){1,7}?(?:of|at)\s+" + _RATIO, "<="),
    (r"\bminimum\s+(?:[\w\-/\u201c\u201d\"]+\s+){1,7}?(?:of|at)\s+" + _NUM + r"(?![\d,]*\s*(?:%|per\s*cent))", ">="),
    (r"\bmaximum\s+(?:[\w\-/\u201c\u201d\"]+\s+){1,7}?(?:of|at)\s+" + _NUM + r"(?![\d,]*\s*(?:%|per\s*cent))", "<="),
    # Wording stress test: "a Debt Service Coverage Ratio of 1.25 or more", "a Leverage Ratio of 3.50 or less".
    (r"\bof\s+" + _NUM + _RATIO_TAIL + r"\s+or\s+(?:more|greater|higher|above)\b", ">="),
    (r"\bof\s+" + _NUM + _RATIO_TAIL + r"\s+or\s+(?:less|lower|below)\b", "<="),
    # Bug hunt 3 (9 November 2026): a term sheet's label form, "Minimum DSCR: 1.15x; Maximum Debt/EBITDA: 5.0x".
    (r"\bminimum\s+(?:[\w\-/\u201c\u201d\"]+\s+){0,6}?[\w\-/\u201c\u201d\")]+\s*:\s*" + _NUM
     + r"(?![\d,]*\s*(?:%|per\s*cent))", ">="),
    (r"\bmaximum\s+(?:[\w\-/\u201c\u201d\"]+\s+){0,6}?[\w\-/\u201c\u201d\")]+\s*:\s*" + _NUM
     + r"(?![\d,]*\s*(?:%|per\s*cent))", "<="),
]
# "Maintain an Effective Debt Service Coverage Ratio as of the end of each calendar quarter of 1.25 to 1.0":
# no comparison word, so the direction comes from the metric (only floors are read this way).
_MAINTAIN = re.compile(r"\bmaintain\b[^.;]{0,160}?\bof\s+" + _RATIO, re.I)
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", re.I)
# A number counting something else is not the limit. Held-out set: "Fixed Charge Coverage Ratio >= 9" was read
# from "to at least 9 decimals" in an assignment form. Also "not less than 30 days".
_NOT_A_LIMIT = re.compile(r"\s*(?:decimal|day|business\s+day|month|year|quarter|week|lender|bank)s?\b", re.I)
# A ratio in words. Held-out set: "a Liability to Equity Ratio of not greater than one-to-one".
_NUMBER_WORDS = {"zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0,
                 "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0}
_DIGIT = "|".join(_NUMBER_WORDS)
_WORD_RATIO = re.compile(
    r"\b(?P<n>" + _DIGIT + r")"
    r"(?:\s+point(?P<dec>(?:\s+(?:" + _DIGIT + r")){1,3})"
    r"|(?P<frac>\s+and\s+(?:one[- ]|a[- ])?(?:half|quarter)|\s+and\s+three[- ]quarters?))?"
    r"[\s-]+to[\s-]+one\b", re.I)


def _word_ratio_value(m: re.Match) -> float:
    """"one and one-quarter to one" is 1.25, "one point two five to one" 1.25, "two and three-quarters" 2.75."""
    value = _NUMBER_WORDS[m.group("n").lower()]
    if m.group("dec"):
        digits = "".join(str(int(_NUMBER_WORDS[w.lower()])) for w in m.group("dec").split())
        value += float("0." + digits)
    frac = (m.group("frac") or "").lower()
    value += 0.75 if "three" in frac else 0.5 if "half" in frac else 0.25 if "quarter" in frac else 0.0
    return value


def _ratio_words_as_digits(text: str) -> str:
    """"three and one-half to one" becomes "3.5      ...  to 1", the same length, so every wording the digit rules
    read ("will not permit ... to be less than", "does not at any time exceed") reads the words too, and match
    positions still point into the original text. Wording stress test: 250 such cases were only flagged."""
    def digits(m: re.Match) -> str:
        number = f"{_word_ratio_value(m):g}"
        return number + " " * (len(m.group(0)) - len(number) - 5) + " to 1"

    def figures(m: re.Match) -> str:
        # Sri Lanka run (Dialog): "not less than one point five (1.5) times": the words, then the figures in brackets
        return m.group(1) + " " * (len(m.group(0)) - len(m.group(1)))

    return _SPELLED_THEN_FIGURES.sub(figures, _WORD_RATIO.sub(digits, text))


_SPELLED_THEN_FIGURES = re.compile(r"\b(?:" + _DIGIT + r")(?:\s+point(?:\s+(?:" + _DIGIT + r")){1,3})?\s*"
                                   r"\((\d+(?:\.\d+)?)\)", re.I)
_SYMBOL_THRESHOLD = re.compile(r"(>=|<=|≥|≤|>|<)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:x\b|times?\b|:1\b)?", re.I)
_FREQUENCY = re.compile(r"\b(monthly|quarterly|semi[-\s]?annual(?:ly)?|half[-\s]?yearly|annual(?:ly)?|yearly)\b", re.I)
_CLAUSE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\.?[ \t]+([^\n]*)$")


@dataclass
class Term:
    metric: str
    category: str  # FINANCIAL | NON_FINANCIAL
    operator: str | None
    threshold: float | None
    unit: str | None
    frequency: str | None  # Core enum: MONTHLY | QUARTERLY | YEARLY | None
    page: int | None
    quote: str
    definition: str | None = None
    confidence: float = 0.0
    method: str = "REGEX"
    issues: list[str] = field(default_factory=list)
    name: str | None = None  # the covenant's own name as written ("Consolidated Senior Secured Leverage Ratio")
    # Phase 3b-1: a step-down schedule ([{"threshold", "from", "to"}], oldest first), the months the agreement tests
    # it in, and the borrower's right to cure a breach (extraction/schedule.py).
    levels: list[dict] | None = None
    test_months: list[int] | None = None
    cure: dict | None = None
    basis: str | None = None  # CONSOLIDATED | STANDALONE when the clause says which figures it is tested on

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Clause:
    number: str
    heading: str
    body: str
    start: int  # offset in the joined text


class _Doc:
    """Pages joined into one text, with offset → page lookup."""

    def __init__(self, pages: list[Page]) -> None:
        parts, self._starts, self._pages = [], [], []
        pos = 0
        for p in pages:
            self._starts.append(pos)
            self._pages.append(p)
            parts.append(p.text.replace("\r\n", "\n"))
            pos += len(parts[-1]) + 1
        self.text = "\n".join(parts)
        self._spans: list[tuple[int, int, str]] | None = None

    def spans(self) -> list[tuple[int, int, str]]:
        """(start, end, metric) of every covenant name in the text, nested ones included."""
        if self._spans is None:
            self._spans = [(m.start(), m.end(), metric) for metric, phrases in _aliases().items()
                           for phrase in phrases for m in re.finditer(_alias_pattern(phrase), self.text, re.I)]
        return self._spans

    def page_at(self, offset: int) -> Page | None:
        found = self.page_span(offset)
        return found[0] if found else None

    def page_span(self, offset: int) -> tuple[Page, int] | None:
        """The page holding ``offset`` and that page's start offset."""
        found = None
        for start, page in zip(self._starts, self._pages, strict=True):
            if start <= offset:
                found = (page, start)
        return found


def compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _best_metric(text: str) -> str | None:
    key = compact(text)
    best, best_len = None, 0
    for metric, phrases in _aliases().items():
        for phrase in phrases:
            c = compact(phrase)
            if len(c) > best_len and c in key:
                best, best_len = metric, len(c)
    return best


def _metric_for_clause(heading: str, body: str) -> str | None:
    if compact(re.split(r"[.(]", heading, maxsplit=1)[0]) in ("EBITDA", "CONSOLIDATEDEBITDA", "MINIMUMEBITDA"):
        return "MIN_EBITDA"  # "7.5 EBITDA. To maintain ... EBITDA of at least ..."
    metric = _best_metric(heading)
    if metric:
        return metric
    first_sentence = body.split(".")[0]
    return _best_metric(first_sentence)


def _frequency(text: str) -> str | None:
    m = _FREQUENCY.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word.startswith("month"):
        return "MONTHLY"
    if word.startswith("quarter"):
        return "QUARTERLY"
    if word.startswith(("annual", "year")):
        return "YEARLY"
    return None  # semi-annual / half-yearly: no Core schedule; the RM sets a custom cron


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _threshold(text: str, amount: bool,
               metric: str | None = None) -> tuple[str, float, str | None, int, int, bool] | None:
    """(operator, value, unit, match start, match end, corroborated) from clause text, or None."""
    if metric in PERCENT_METRICS:
        return _percent_threshold(text)
    if not amount:
        text = _ratio_words_as_digits(text)
    words = []
    for pattern, op in _WORD_THRESHOLDS:
        m = re.search(pattern, text, re.I)
        if m and not _NOT_A_LIMIT.match(text, m.end()):
            words.append((m.start(), op, _number(m.group(1)), (m.group(2) or "").strip().lower() or None, m))
    for pattern, op in _TABLE_THRESHOLDS + _NAMED_THRESHOLDS:
        m = re.search(pattern, text, re.I)
        if m and not _NOT_A_LIMIT.match(text, m.end()):
            raw = next(g for g in m.groups() if g)
            unit = "usd" if "$" in m.group(0)[-len(raw) - 3:] else None
            words.append((m.start(), op, _number(raw), unit, m))
    if metric in FLOOR_METRICS and not amount:
        m = _MAINTAIN.search(text)
        if m:
            words.append((m.start(), ">=", _number(m.group(1)), None, m))
    words.sort(key=lambda w: w[0])
    symbol = None if amount else _SYMBOL_THRESHOLD.search(text)
    if symbol:
        op = {"≥": ">=", "≤": "<="}.get(symbol.group(1), symbol.group(1))
        value = _number(symbol.group(2))
        agree = any(w[1] == op and abs(w[2] - value) < 1e-9 for w in words)
        anchor = words[0][4] if words else symbol
        return op, value, None, anchor.start(), anchor.end(), agree
    if words:
        _, op, value, unit, m = words[0]
        if amount and not unit and value < 1000:
            return None  # a ratio-sized number is not a net-worth floor
        if metric in _GEARING and re.match(r"\s*(?:%|per\s*cent\b|percent\b)", text[m.end():], re.I):
            # Sri Lanka run: "Gearing Ratio not exceeding 100%" is a debt-to-equity of 1.0
            pct = re.match(r"\s*(?:%|per\s*cent\b|percent\b)", text[m.end():], re.I)
            return op, value / 100, None, m.start(), m.end() + pct.end(), False
        return op, value, unit, m.start(), m.end(), False
    if amount:  # "a minimum Net Worth of INR 5,000 Crores": a currency amount alone is a floor
        m = re.search(_MONEY + r"\s*" + _NUM + _UNIT, text, re.I)
        if m:
            return ">=", _number(m.group(1)), (m.group(2) or "").strip().lower() or None, m.start(), m.end(), False
    return None


_GEARING = {"DEBT_TO_EQUITY", "DEBT_TO_NET_WORTH"}


def _percent_threshold(text: str) -> tuple[str, float, str | None, int, int, bool] | None:
    """A loan-to-value limit: the first percentage after the name; a ceiling unless the words say a floor."""
    m = _PERCENT.search(text)
    if not m or float(m.group(1)) > 100:
        return None
    before = text[max(0, m.start() - 120):m.start()]
    floor = re.search(r"at\s+least|not\s+less\s+than|minimum", before, re.I) and not re.search(
        r"exceed|more\s+than|or\s+less|maximum", before + text[m.end():m.end() + 12], re.I)
    return (">=" if floor else "<="), float(m.group(1)), "%", m.start(), m.end(), False


def _quote(doc: _Doc, start: int, end: int) -> tuple[str, int | None]:
    """The line before and the line(s) of [start, end), clipped to the page holding ``end`` and 300 chars.

    Clipping keeps the quote verbatim on one page (the citation check) while still containing the number.
    """
    span = doc.page_span(max(start, end - 1))
    if span is None:
        return "", None
    page, page_start = span
    line_start = max(page_start, doc.text.rfind("\n", 0, start) + 1)
    prev = max(page_start, doc.text.rfind("\n", 0, max(0, line_start - 1)) + 1)
    line_end = doc.text.find("\n", end)
    line_end = len(doc.text) if line_end == -1 else line_end
    quote = normalize_ws(doc.text[prev:line_end])
    if len(quote) > 300:
        # A paragraph with no line breaks (HTML filings in the held-out set): keep the words around the match. The
        # line's last 300 characters could be sentences later, so the reviewer saw unrelated text.
        lo = max(line_start, start - 100)
        if end - lo > 300:
            lo = end - 300
        quote = normalize_ws(doc.text[lo:min(line_end, max(end, lo + 300))]).strip()
    return quote, page.no


def _definition(body: str, metric: str) -> str | None:
    for line in body.splitlines():
        if "=" in line and not re.search(r"[<>]=?", line) and re.search(r"[/*+-]", line):
            lhs = compact(line.split("=")[0])
            if lhs and any(compact(a) in lhs or lhs in compact(a) for a in _aliases().get(metric, [metric])
                           + [metric]):
                return normalize_ws(line)
    return None


def _clauses(doc: _Doc) -> list[_Clause]:
    marks = list(_CLAUSE.finditer(doc.text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(doc.text)
        body = doc.text[m.end():end]
        body = re.split(r"\n[ \t=]*(?:ARTICLE|SCHEDULE)\s+\w+", body, maxsplit=1, flags=re.I)[0]
        out.append(_Clause(m.group(1), m.group(2).strip(), body, m.start()))
    return out


def _name_key(name: str) -> str:
    """Two names for one covenant ("Total Leverage Ratio", "Consolidated Total Leverage Ratio") share a key."""
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower())
             if w not in ("the", "consolidated", "total", "minimum", "maximum", "ratio", "a", "an", "of")]
    return " ".join(words)


def _add(terms: list[Term], term: Term) -> bool:
    """Keeps a second covenant of a family only when it has its own name and its own number."""
    same = [t for t in terms if t.metric == term.metric]
    if not same:
        terms.append(term)
        return True
    named = next((t for t in same if _name_key(t.name) == _name_key(term.name)), None)
    if named is not None:
        # "the Senior Secured Leverage Ratio is less than 1.30" (a condition, often in the definitions) gives way to
        # the covenant itself, "will not permit the Senior Secured Leverage Ratio ... to exceed 3.50".
        if named.operator in ("<", ">") and term.operator in ("<=", ">="):
            terms[terms.index(named)] = term
            return True
        return False
    if len(same) >= MAX_PER_METRIC:
        return False
    if any((t.operator, t.threshold) == (term.operator, term.threshold) for t in same):
        return False  # the same covenant named twice
    terms.append(term)
    return True


def _clause_name(heading: str, metric: str) -> str:
    name = normalize_ws(re.split(r"[.(\u201c\"]", heading, maxsplit=1)[0])
    return name if _best_metric(name) == metric or metric == "MIN_EBITDA" else metric.replace("_", " ").title()


def _others(doc: _Doc, metric: str, name: tuple[int, int]) -> list[tuple[int, int]]:
    """Names of other metrics: not nested in a longer name, and not overlapping this metric's name."""
    spans = doc.spans()
    return [(s, e) for s, e, o in spans if o != metric and not (s < name[1] and e > name[0])
            and not any(s2 <= s and e2 >= e and e2 - s2 > e - s for s2, e2, _ in spans)]


def _own_part(doc: _Doc, metric: str, name: tuple[int, int], lo: int, hi: int) -> tuple[int, int]:
    """The text around one name that belongs to it: from the end of the other metric named before it to the start
    of the one named after it. "a DSCR of not less than 1.25:1 and a Leverage Ratio of not more than 3.50:1" gives
    each ratio its own number (two-ratio clauses read the first number for both)."""
    for s, e in _others(doc, metric, name):
        if lo < e <= name[0]:
            lo = e
        if name[1] <= s < hi:
            hi = s
    digits = [m.end() for m in re.finditer(r"\d", doc.text[lo:name[0]])]
    return lo + (digits[-1] if digits else 0), hi  # the number before the name is the other metric's


def _own_threshold(doc: _Doc, metric: str, offset: int,
                   combined: str) -> tuple[str, float, str | None, int, int, bool] | None:
    """In a clause that names several metrics, the threshold in this metric's own part of it, or None."""
    end = offset + len(combined)
    names = sorted((s, e) for s, e, o in doc.spans() if o == metric and offset <= s < end)
    names = [n for n in names if not any(s <= n[0] and e >= n[1] and e - s > n[1] - n[0]
                                         for s, e, o in doc.spans() if o != metric)]
    if not names or not any(offset <= s < end for s, _ in _others(doc, metric, names[0])):
        return None  # one metric in the clause: the clause is its own part
    for name in names:
        lo, hi = _own_part(doc, metric, name, offset, end)
        lead = doc.text[offset:lo]
        prefix = "will not permit " if _PERMIT_LEAD.search(lead) else "maintain " if re.search(
            r"\bmaintain\b", lead, re.I) else ""
        found = _threshold(prefix + doc.text[lo:hi], metric in AMOUNT_METRICS, metric)
        if found:
            op, value, unit, s, e, agree = found
            base = lo - offset - len(prefix)
            return op, value, unit, max(lo - offset, base + s), base + e, agree
    return None


def _financial(doc: _Doc, clauses: list[_Clause]) -> list[Term]:
    terms: list[Term] = []
    for c in clauses:
        metric = _metric_for_clause(c.heading, c.body)
        if not metric:
            continue
        combined = f"{c.heading}\n{c.body}"
        body_offset = c.start + (doc.text[c.start:].find(c.heading) if c.heading else 0)
        found = _own_threshold(doc, metric, body_offset, combined) or _threshold(combined, metric in AMOUNT_METRICS,
                                                                                 metric)
        if not found or not _plausible(metric, found[1], combined[max(0, found[3] - 300):found[4] + 200]):
            continue
        op, value, unit, s, e, corroborated = found
        quote, page = _quote(doc, body_offset + s, body_offset + e)
        _add(terms, Term(metric, "FINANCIAL", op, value, unit, _frequency(combined), page, quote,
                         _definition(c.body, metric), 0.95 if corroborated else 0.85,
                         name=_clause_name(c.heading, metric)))
    _scan_unnumbered(doc, terms)
    _scan_written_out(doc, terms)
    _scan_ebitda_floor(doc, terms)
    _scan_defined(doc, terms)
    return terms


# Thresholds given by a defined word (held-out set, a railcar term loan): the covenant says "If the Loan-to-Value
# Ratio ... exceeds the LTV Maximum Ratio" and the definitions say "'LTV Maximum Ratio' means 73.0%", or "'DSCR
# Minimum' means one point one five to one (1.15:1.00)". The covenant sentence holds no number, so neither the
# rules nor the safety net saw it.
_QUOTE_MARK = "\"“”�"
_MEANS = re.compile(r"[" + _QUOTE_MARK + r"]\s+(?:means|shall\s+mean)\b")
_DEFINED = re.compile(r"[" + _QUOTE_MARK + r"]([A-Z][^" + _QUOTE_MARK + r"\n]{2,60})[" + _QUOTE_MARK + r"]\s+"
                      r"(?:means|shall\s+mean)\s+((?:[^.;]|\.(?=\d)){0,160})")
# A definition that is a formula or depends on something else is not one fixed number.
_FORMULA = re.compile(r"greater\s+of|lesser\s+of|sum\s+of|\bplus\b|\bminus\b|multiplied|divided|ratio\s+of|"
                      r"\bless\b|aggregate|amount\s+equal|set\s+forth|specified\s+in|determined", re.I)
_DEFINED_VALUE = [
    (re.compile(r"\((\d+(?:\.\d+)?)\s*(?::|to)\s*1(?:\.0+)?\)|^(\d+(?:\.\d+)?)\s*(?::|to)\s*1(?:\.0+)?\b", re.I), None),
    (re.compile(r"^(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", re.I), "%"),
    (re.compile(r"^(?:\$|USD|US\$|INR|Rs\.?|₹|LKR)\s*([\d,]+(?:\.\d+)?)(?!\s*(?:million|billion|crores?|lakhs?)\b)",
                re.I), "usd"),
    # "'DSCR Minimum' means 1.25x" / "1.25 times" / "1.25" (wording stress test).
    (re.compile(r"^(\d{1,2}(?:\.\d+)?)\s*(?:x\b|times\b)?\s*(?:$|[,;(])", re.I), None),
]
_SCALED_AMOUNT = re.compile(r"^(?:\$|USD|US\$|INR|Rs\.?|₹|LKR)\s*([\d,]+(?:\.\d+)?)\s*"
                            r"(million|billion|crores?|lakhs?)\b", re.I)
# "'DSCR Minimum' means one and one-quarter to one" (no figures at all).
_WORDS_ONLY = re.compile(r"^(?P<n>" + _DIGIT + r")(?:\s+point(?P<dec>(?:\s+(?:" + _DIGIT + r")){1,3})"
                         r"|(?P<frac>\s+and\s+(?:one[- ]|a[- ])?(?:half|quarter)|\s+and\s+three[- ]quarters?))?"
                         r"[\s-]+to[\s-]+one\b", re.I)
# The word just before the defined name says which way the limit points: a breach ("exceeds the LTV Maximum Ratio")
# and an obligation ("not exceed the LTV Maximum Ratio") both make it a ceiling.
_DEFINED_COMPARISON = [
    (r"less\s+than\s+or\s+equal\s+to|equal\s+to\s+or\s+less\s+than|not\s+(?:to\s+)?exceed|not\s+(?:be\s+)?"
     r"(?:greater|more)\s+than|no\s+(?:greater|more)\s+than|at\s+most", "<="),
    (r"greater\s+than\s+or\s+equal\s+to|equal\s+to\s+or\s+greater\s+than|not\s+(?:be\s+)?(?:less|lower)\s+than|"
     r"no\s+less\s+than|at\s+least", ">="),
    (r"exceeds?|(?:is|be)\s+(?:greater|more|higher)\s+than|above", "<="),
    (r"(?:is|be)\s+(?:less|lower)\s+than|(?:falls?\s+)?below", ">="),
]


def _defined_numbers(doc: _Doc) -> dict[str, tuple[float, str | None, str]]:
    """Defined words whose whole meaning is one number: name -> (value, unit, definition text)."""
    out: dict[str, tuple[float, str | None, str]] = {}
    for m in _DEFINED.finditer(doc.text):
        name, meaning = normalize_ws(m.group(1)), normalize_ws(m.group(2))
        if _FORMULA.search(meaning) or not _best_metric(name) and not re.search(r"ratio|minimum|maximum", name, re.I):
            continue
        if re.search(r"sweep|pricing|margin|step[- ]?(?:down|up)|incurrence", name, re.I):
            continue  # "LTV Cash Sweep Ratio Threshold": a trigger for a cash sweep, not the covenant
        scaled = _SCALED_AMOUNT.match(meaning)
        if scaled:  # "$50.0 million", "INR 500 crore": the amount and its scale, like a covenant written out
            scale = scaled.group(2).lower().rstrip("s")
            out.setdefault(name, (_number(scaled.group(1)), scale, meaning))
            continue
        # "one point one five to one (1.15:1.00)": the figures may follow the words.
        for pattern, unit in _DEFINED_VALUE:
            v = pattern.search(meaning)
            if v:
                out.setdefault(name, (_number(next(g for g in v.groups() if g)), unit, meaning))
                break
        else:
            w = _WORDS_ONLY.search(meaning)
            if w:
                out.setdefault(name, (_word_ratio_value(w), None, meaning))
    return out


def _scan_defined(doc: _Doc, terms: list[Term]) -> None:
    for name, (value, unit, meaning) in _defined_numbers(doc).items():
        for m in re.finditer(r"\b(?:the\s+)?" + re.escape(name) + r"\b", doc.text):
            before = doc.text[max(0, m.start() - 60):m.start()]
            if doc.text[m.end():m.end() + 3].lstrip().startswith(tuple(_QUOTE_MARK)) or \
                    doc.text[max(0, m.start() - 1):m.start()] in _QUOTE_MARK:
                continue  # the definition itself
            op = next((o for p, o in _DEFINED_COMPARISON if re.search(r"(?:" + p + r")\s+(?:the\s+)?$", before, re.I)),
                      None)
            if op is None:
                continue
            lead = doc.text[max(0, m.start() - 300):m.start()]
            metric = _best_metric(name) or _best_metric(lead[-200:])
            context = doc.text[max(0, m.start() - 300):m.end() + 200]
            if not metric or not _plausible(metric, value, context):
                continue
            if (metric in PERCENT_METRICS) != (unit == "%") or (metric in AMOUNT_METRICS) != (unit not in (None, "%")):
                continue
            if unit == "usd":
                unit = None  # an amount in the currency as written, like the other amount covenants
            quote, page = _quote(doc, max(0, m.start() - 120), m.end())
            _add(terms, Term(metric, "FINANCIAL", op, value, unit, _frequency(context), page, quote, None, 0.7,
                             issues=[f"THRESHOLD_FROM_DEFINITION: '{name}' means {meaning[:80]}"], name=name))
            break  # the first comparison with the name is the covenant; later ones are cures and cash sweeps


def _plausible(metric: str, value: float, context: str) -> bool:
    """A maintenance covenant: a sensible number, not a pricing, incurrence or release condition."""
    if metric not in AMOUNT_METRICS | PERCENT_METRICS and value > MAX_RATIO:
        return False
    return not _NOT_A_COVENANT.search(context)


def _alias_pattern(phrase: str) -> str:
    # "Total Debt / EBITDA" is the alias "DEBT/EBITDA" with spaces around the slash (bug hunt 3).
    return r"\b" + r"[\s_\-]*".join(re.escape(w).replace("/", r"\s*/\s*") for w in phrase.split()) + r"\b"


# "will not permit the Leverage Ratio ... to exceed 3.50", and the list form "the Borrower will not: ... 6.01
# Financial Covenant. Permit the Leverage Ratio to be greater than 7.3": the negation sits before the name.
# Held-out HA08: "Section 6.2 Financial Covenants. Permit: (a) Total Leverage Ratio to be greater than 0.75 ...
# (b) Tangible Net Worth to be less than $1,000,000,000": the list heading, with the "shall not" far above it.
_PERMIT_LEAD = re.compile(r"(?:shall|will)\s+not\s+permit\b[^.;]*$|(?:shall|will)\s+not\s*(?:,[^:;]{0,120})?:"
                          r"[\s\S]{0,300}?\bpermit\b[^.;]*$"  # "shall not: (a) ...; or (b) permit the ..."
                          r"|(?-i:\bPermit)\s*:[^;]{0,400}$", re.I)


def _scan_unnumbered(doc: _Doc, terms: list[Term]) -> None:
    """Fallback for covenants written outside numbered clauses (tables, schedules, net-worth floors)."""
    # Every alias match in the text, so a match inside a longer alias of another metric ("Debt to EBITDA" in
    # "Net Debt to EBITDA") is not taken for a covenant of its own.
    spans = doc.spans()
    for metric, phrases in _aliases().items():
        if metric == "MIN_EBITDA":
            continue  # only a clause headed "EBITDA": the word is in every definition
        for phrase in sorted(phrases, key=len, reverse=True):
            for m in re.finditer(_alias_pattern(phrase), doc.text, re.I):
                if any(o != metric and s <= m.start() and e >= m.end() and e - s > m.end() - m.start()
                       for s, e, o in spans):
                    continue
                before = doc.text[max(0, m.start() - 20):m.start()].upper()
                if metric in ("MIN_NET_WORTH", "MIN_TANGIBLE_NET_WORTH") and ("DEBT" in before or "TANGIBLE" in before
                                                                            and metric == "MIN_NET_WORTH"):
                    continue
                window = doc.text[m.end():m.end() + 160]
                if re.search(r"set\s+forth|indicated|specified", window, re.I):
                    window = doc.text[m.end():m.end() + 420]  # a step-down table follows the words
                window = re.split(r"\n\s*\n", window, maxsplit=1)[0]
                lo, hi = _own_part(doc, metric, (m.start(), m.end()), m.start() - 80, m.end() + len(window))
                window = window[:hi - m.end()]  # "... and a Leverage Ratio of 3.50": not this metric's number
                lead = doc.text[max(0, m.start() - 420):m.start()]
                prefix = "will not permit " if _PERMIT_LEAD.search(lead) else ""
                # "maintain a minimum Interest Coverage of 1.25:1": the words before the name, in this sentence
                tail = re.split(r"[.;:\n]", lead[-80:])[-1]
                tail = tail[len(tail) - (m.start() - lo):] if m.start() - lo < len(tail) else tail
                combined = prefix + tail + m.group(0) + window
                found = _threshold(combined, metric in AMOUNT_METRICS, metric)
                context = doc.text[max(0, m.start() - 300):m.end() + 200]
                if not found or not _plausible(metric, found[1], context):
                    continue
                means = _MEANS.search(combined, len(prefix + tail + m.group(0)), found[4])
                own = means and re.match(r"(?:[^.;" + _QUOTE_MARK + r"]|\.(?=\d)){0,160}", combined[means.end():])
                if own and not _FORMULA.search(own.group(0)):
                    # A definition that is one number ("'LTV Cash Sweep Ratio Threshold' means 71.0%") is not the
                    # covenant; _scan_defined reads it where a covenant uses it. A formula definition ("'Minimum
                    # Tangible Net Worth' shall mean the sum of $105,000,000 and ...") is still read here.
                    continue
                op, value, unit, s, e, _ = found
                if metric == "MIN_NET_WORTH" and re.search(r"tangible\s+net\s+worth",
                                                           combined[len(prefix + tail + m.group(0)):e], re.I):
                    continue  # bug hunt: a "Net Worth" heading over "a Tangible Net Worth of ..." is one covenant
                if metric == "MINIMUM_LIQUIDITY" and op in ("<=", "<"):
                    continue  # "minus Unrestricted Cash ... not exceeding $50,000,000": a cap in a definition
                end = m.start() - len(tail) + e - len(prefix)
                quote, page = _quote(doc, m.start(), max(end, m.end()))
                words = re.search(r"((?:[A-Z][\w\-]*\s+){0,3})$", doc.text[max(0, m.start() - 60):m.start()])
                name = normalize_ws((words.group(1) if words else "") + m.group(0))
                _add(terms, Term(metric, "FINANCIAL", op, value, unit, _frequency(window), page, quote, None, 0.7,
                                 name=name))


# Sri Lanka run (10 November 2026): ratios written out as a sentence, with no ratio name to find.
_DEBT = r"\b(?P<debt>(?:consolidated\s+)?(?:total\s+|net\s+)?(?:debt|borrowings))\b"
_CAP = r"(?:shall|will|must|does|should)?\s*not\s+(?:at\s+any\s+time\s+)?(?:to\s+)?exceed(?:ing)?\s+"
_WRITTEN_OUT = [
    # "Consolidated Total Debt shall not exceed 2.0 times Consolidated EBITDA"
    (re.compile(_DEBT + r"[^.;]{0,60}?" + _CAP + _NUM + r"\s*(?:x|times)\s+(?:the\s+)?(?:consolidated\s+)?"
                r"(?:adjusted\s+)?EBITDA\b", re.I), "LEVERAGE", 1.0),
    # "its Total Debt does not exceed 50% of its Total Equity"
    (re.compile(_DEBT + r"[^.;]{0,60}?" + _CAP + _NUM + r"\s*(?:%|per\s*cent\b|percent\b)\s+of\s+(?:its\s+|the\s+)?"
                r"(?:total\s+)?(?:shareholders['’]?\s+)?(?:equity|net\s+worth)\b", re.I), "GEARING", 0.01),
    # "The ratio of total borrowings of the Borrower (on a company basis) to its net worth shall not ... exceed 0.50:1"
    (re.compile(r"\bratio\s+of\s+(?:the\s+)?" + _DEBT + r"[^.;]{0,80}?\bto\s+(?:its\s+|the\s+)?(?:total\s+)?"
                r"(?:tangible\s+)?(?:equity|net\s+worth)\b[^.;]{0,60}?" + _CAP + _NUM, re.I), "GEARING", 1.0),
]


def _scan_written_out(doc: _Doc, terms: list[Term]) -> None:
    for pattern, kind, scale in _WRITTEN_OUT:
        for m in pattern.finditer(doc.text):
            if _NOT_A_COVENANT.search(doc.text[max(0, m.start() - 300):m.end() + 200]):
                continue
            net = m.group("debt").lower().startswith(("net", "consolidated net"))
            metric = ("NET_LEVERAGE_RATIO" if net else "DEBT_TO_EBITDA") if kind == "LEVERAGE" else "DEBT_TO_EQUITY"
            value = _number(m.group(m.lastindex)) * scale
            if value > MAX_RATIO:
                continue
            quote, page = _quote(doc, m.start(), m.end())
            _add(terms, Term(metric, "FINANCIAL", "<=", value, None, _frequency(m.group(0)), page, quote, None, 0.7,
                             name=normalize_ws(m.group(0))[:60]))


# "The Borrower shall maintain a Consolidated EBITDA of not less than $50,000,000" outside a clause headed "EBITDA"
# (wording stress test: 141 of 162 such floors were only shown as possible covenants). The word is in every
# definition and pricing grid, so only a floor with an amount, in the same sentence as the name, is read.
_EBITDA_NAME = re.compile(r"\b(?:minimum\s+)?(?:consolidated\s+)?(?:adjusted\s+)?EBITDA\b", re.I)
_SENTENCE_END = re.compile(r";|(?<!Rs)\.(?!\d)|\n\s*\n")  # "Rs. 500 Crores" and "EBITDA: not less than" go on


def _scan_ebitda_floor(doc: _Doc, terms: list[Term]) -> None:
    if any(t.metric == "MIN_EBITDA" for t in terms):
        return
    others = [(s, e) for s, e, o in doc.spans() if o != "MIN_EBITDA"]
    for m in _EBITDA_NAME.finditer(doc.text):
        if any(s <= m.start() and e >= m.end() for s, e in others):
            continue  # "Total Debt to EBITDA": another metric
        after = doc.text[m.end():m.end() + 200]
        if re.match(r"\s*(?:[\"\u201c\u201d]|means\b|shall\s+mean\b|has\s+the\s+meaning\b)", after, re.I):
            continue  # the definition
        end = _SENTENCE_END.search(after)
        window = after[:end.start()] if end else after
        lead = doc.text[max(0, m.start() - 420):m.start()]
        prefix = "will not permit " if _PERMIT_LEAD.search(lead) else ""
        tail = re.split(r"[.;:\n]", lead[-80:])[-1]
        tail = tail[max((d.end() for d in re.finditer(r"[\d%]", tail)), default=0):]  # no number from before it
        # Held-out set: "the greater of $115,000,000 and 30% of LTM EBITDA" (a basket) and "Consolidated EBITDA
        # ... will be deemed to be $91,400,000" (a figure for a past quarter) are not floors.
        if re.search(r"\bdeemed\b|\b(?:greater|lesser)\s+of\b", tail + window, re.I):
            continue
        combined = prefix + tail + m.group(0) + window
        found = _threshold(combined, True, "MIN_EBITDA")
        context = doc.text[max(0, m.start() - 300):m.end() + 200]
        if not found or found[0] != ">=" or found[4] <= len(prefix + tail + m.group(0)) or \
                not _plausible("MIN_EBITDA", found[1], context):
            continue  # "greater than $100,000,000" is a pricing level, not a floor
        op, value, unit, _, e, _ = found
        quote, page = _quote(doc, m.start(), max(m.start() - len(tail) + e - len(prefix), m.end()))
        _add(terms, Term("MIN_EBITDA", "FINANCIAL", op, value, unit, _frequency(window), page, quote, None, 0.7,
                         name=normalize_ws(m.group(0))))
        return


# Covenants on the security and on asset sales, which no ratio alias names (review RISK-01): "the value of the
# Collateral is not less than INR 10,000 Crores", "security cover of at least 1.25 times", "the value of the
# substitute collateral shall be at least 120% of ...", "assets having a value not exceeding INR 50 Crores in any
# Financial Year". Each is held for a person (confidence below the publish floor): whether such a clause is an
# ongoing covenant or a one-off condition is a credit judgement.
_AMOUNT = r"(?:INR|Rs\.?|₹)\s*" + _NUM + r"\s*(crores?|cr\b|lakhs?|lacs?)"
_FLOOR = r"(?:not\s+(?:be\s+)?less\s+than|at\s+least|minimum\s+of|no\s+less\s+than)"
_SECURITY_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    # metric, operator, unit kind ("amount", "percent", "times"), pattern with the number in group 1
    ("SECURITY_COVER", ">=", "percent", re.compile(
        r"\b(?:value\s+of\s+the\s+(?:substitute\s+)?(?:collateral|security)|(?:security|asset|collateral)\s+cover)"
        r"[^.]{0,80}?" + _FLOOR + r"\s*" + _NUM + r"\s*(?:%|per\s*cent)", re.I)),
    ("SECURITY_COVER", ">=", "times", re.compile(
        r"\b(?:security|asset|collateral)\s+cover[^.]{0,80}?" + _FLOOR + r"\s*" + _NUM + r"\s*(?:x\b|times)", re.I)),
    ("MIN_COLLATERAL_VALUE", ">=", "amount", re.compile(
        r"\bvalue\s+of\s+the\s+(?:collateral|security)[^.%]{0,60}?" + _FLOOR + r"\s*" + _AMOUNT, re.I)),
    ("ASSET_DISPOSAL_CAP", "<=", "amount", re.compile(
        r"\b(?:dispos\w*|sell|sale|transfer)\b[^.]{0,220}?(?:not\s+exceeding|up\s+to|not\s+more\s+than)\s*"
        + _AMOUNT, re.I)),
]


def _security_terms(doc: _Doc, found: set[str]) -> list[Term]:
    out: list[Term] = []
    for metric, op, kind, pattern in _SECURITY_RULES:
        if metric in found:
            continue
        m = pattern.search(doc.text)
        if not m:
            continue
        unit = {"percent": "%", "times": "x"}.get(kind) or (m.group(2) or "").strip().lower()
        quote, page = _quote(doc, m.start(), m.end())
        out.append(Term(metric, "FINANCIAL", op, _number(m.group(1)), unit, _frequency(m.group(0)), page, quote,
                        None, 0.75))
        found.add(metric)
    return out


# "On a Change of Control the Borrower shall prepay the Loans": a payment that a change of control triggers (loan
# mechanics), not an undertaking about control (Codex acceptance case "mandatory_prepayment", 19 Nov 2026).
_TRIGGERED_PAYMENT = re.compile(r"\b(?:prepay|prepayment|repay|repayment|cancel(?:led|lation)?|"
                                r"become\s+(?:immediately\s+)?due)\b", re.I)


def _control_is_a_payment_trigger(metric: str, text: str) -> bool:
    if metric != "CHANGE_OF_CONTROL" or not _TRIGGERED_PAYMENT.search(text):
        return False
    return not re.search(r"\b(?:shall|will)\s+not\b[^.]{0,80}?\b(?:change|permit|allow|cause)\b", text, re.I)


def _non_financial(doc: _Doc, clauses: list[_Clause]) -> list[Term]:
    out: dict[str, Term] = {}
    for c in clauses:
        heading = normalize_ws(c.heading)
        context = normalize_ws(f"{c.heading} {c.body}")
        for metric, patterns, heading_patterns in NON_FINANCIAL_RULES:
            if metric in out:
                continue
            heading_hit = any(re.search(p, heading, re.I) for p in heading_patterns)
            if not heading_hit and not _INTENT.search(context):
                continue
            if not any(re.search(p, context, re.I) for p in patterns):
                continue
            if metric == "INSURANCE_MAINTENANCE" and not re.search(
                    r"\b(?:maintain|maintained|keep|kept|renew|renewed|continue|continued)\b", context, re.I):
                continue
            if _control_is_a_payment_trigger(metric, context):
                continue
            quote, page = _quote(doc, c.start, c.start + len(c.heading) + 1)
            out[metric] = Term(metric, "NON_FINANCIAL", None, None, None, _frequency(context), page, quote,
                               context[:320], 0.8)
    # Not in a numbered clause ("(b) The Borrower shall not create any Lien ..." or a plain paragraph): the first
    # sentence that says it, with less confidence, so a person confirms it.
    for m in re.finditer(r"[^.\n][^.]{20,600}?(?:\.(?!\d)|$)", doc.text):
        sentence = normalize_ws(m.group(0))
        if not _INTENT.search(sentence) and not re.search(r"\bchange\s+of\s+control\b", sentence, re.I):
            continue
        for metric, patterns, _ in NON_FINANCIAL_RULES:
            if metric in out or not any(re.search(p, sentence, re.I) for p in patterns):
                continue
            if metric == "INSURANCE_MAINTENANCE" and not re.search(r"\b(?:maintain|keep|kept|insured|renew)\b",
                                                                   sentence, re.I):
                continue
            if _control_is_a_payment_trigger(metric, sentence):
                continue
            quote, page = _quote(doc, m.start(), m.end())
            out[metric] = Term(metric, "NON_FINANCIAL", None, None, None, _frequency(sentence), page, quote,
                               sentence[:320], 0.7)
    return list(out.values())


def extract_terms(pages: list[Page]) -> list[Term]:
    """All covenant terms in the agreement: financial (with thresholds) then non-financial."""
    doc = _Doc(pages)
    if not doc.text.strip():
        return []
    clauses = _clauses(doc)
    financial = _financial(doc, clauses)
    for t in financial:
        _flag_other_levels(t)
    _schedules(pages, doc, financial)
    security = _security_terms(doc, {t.metric for t in financial})
    have = {t.metric for t in financial + security}
    # A disposal cap ("not dispose of assets ... other than assets not exceeding INR 25 crore") already is the
    # restriction: one covenant, not two.
    other = [t for t in _non_financial(doc, clauses)
             if not (t.metric == "ASSET_DISPOSAL_RESTRICTION" and "ASSET_DISPOSAL_CAP" in have)]
    return financial + security + other


_LEVEL = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:x\b|times\b|:\s*1(?:\.0+)?(?!\.?\d)|to\s*1(?:\.0+)?(?!\.?\d))",
                    re.I)


_INPUT_NAMES = {"EBITDA": "EBITDA", "DEBT": "Debt", "NET_WORTH": "Net Worth", "INTEREST": "Interest"}


def _schedules(pages: list[Page], doc: _Doc, financial: list[Term]) -> None:
    """Phase 3b-1: each ratio covenant's step-down schedule (read from its clause and what follows it on the page),
    and, for every financial covenant, the agreement's test dates and cure right. Each is shown to a person."""
    from dps.extraction.schedule import (
        INPUTS,
        adjustments,
        basis,
        cure,
        definitions,
        frequency_for,
        step_down,
        test_months,
    )

    by_page = {p.no: p.text or "" for p in pages}
    months = test_months(doc.text)
    right = cure(doc.text)
    defined = definitions(doc.text)
    for t in financial:
        if t.threshold is None or t.operator is None:
            continue
        # The basis is read from the sentence that sets the limit, not a neighbouring clause the quote takes in.
        sentences = [s for s in re.split(r"(?<!\d)\.(?!\d)\s+", t.quote or "") if s.strip()]
        t.basis = basis(sentences[-1] if sentences else "")
        # The agreement's own EBITDA, Debt, Net Worth or Interest, when it adds back, leaves out or caps: the bank's
        # standard figure is what DPS reads, so a person checks the covenant still means what it says.
        for key in INPUTS.get(t.metric.split(":")[0], ()):
            changes = adjustments(defined.get(key, ""))
            if changes:
                t.issues.append(f"DEFINITION_DIFFERS: the agreement's {_INPUT_NAMES[key]} is adjusted "
                                f"('{changes[0][:110]}'); the statement's figure may need the same adjustment")
        if t.metric not in AMOUNT_METRICS | PERCENT_METRICS and t.page in by_page:
            text = by_page[t.page] + "\n" + by_page.get(t.page + 1, "")[:800]
            head = normalize_ws(t.quote or "")[:60]
            flat = normalize_ws(text)
            at = flat.find(head) if head else -1
            levels = step_down(flat[at:at + 2000] if at >= 0 else "", t.threshold)
            if levels:
                t.levels = [lv.as_dict() for lv in levels]
                t.issues = [i for i in t.issues if not i.startswith("SEVERAL_LEVELS")]
                steps = ", then ".join(f"{lv.threshold:g}" + (f" to {lv.to_date}" if lv.to_date else " after")
                                       for lv in levels)
                t.issues.append(f"STEP_DOWN: {len(levels)} levels read ({steps}); check the dates before Apply")
        if months:  # shown with the covenant; not a reason to hold it
            t.test_months = months
            t.frequency = frequency_for(months)
        if right is not None:
            t.cure = right.as_dict()



def _flag_other_levels(t: Term) -> None:
    """A clause that gives more than one level ("4.00 to 1.00 ..., and thereafter 3.50 to 1.00", a step-down
    schedule) holds the first one; a person picks the level that applies now (wording stress test)."""
    if t.threshold is None or t.metric in AMOUNT_METRICS | PERCENT_METRICS:
        return
    quote = _Doc([Page(1, t.quote or "", "TEXT", 1.0)])
    names = [(s, e) for s, e, o in quote.spans() if o == t.metric]
    text = quote.text
    if names and _others(quote, t.metric, names[0]):
        # "a DSCR of 1.25:1 and a Leverage Ratio of 3.50:1": 3.50 is the other covenant's, not a second level
        text = " ".join(quote.text[lo:hi] for lo, hi in (_own_part(quote, t.metric, n, 0, len(text)) for n in names))
    others = sorted({float(m.group(1)) for m in _LEVEL.finditer(text)
                     if abs(float(m.group(1)) - t.threshold) > 1e-9})
    if others:
        t.issues.append("SEVERAL_LEVELS: the clause also gives " + ", ".join(f"{v:g}" for v in others))
