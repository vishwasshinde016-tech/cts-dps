"""Flow B Extract/Calculate steps: financial statement line items (04-api-contracts.md §6.2).

Reads label/value rows from PDF text, XLSX or CSV, maps labels to the 20 catalog keys, converts amounts to
INR crore, then derives missing inputs from the ones present (e.g. TOTAL_DEBT = ST_DEBT + LT_DEBT).
"""

from __future__ import annotations

import contextlib
import contextvars
import csv
import io
import re
from dataclasses import asdict, dataclass, field

from dps.money import CODE_WORDS
from dps.parse.engine import ParseError

# Catalog key → label phrases (lower case, matched on whole words; longest phrase wins).
LINE_ALIASES: dict[str, list[str]] = {
    "REVENUE": ["revenue from operations", "total revenue", "net sales", "revenue", "turnover"],
    "EBITDA": ["ebitda", "operating profit before depreciation"],
    "EBIT": ["ebit", "operating profit"],
    "DEPRECIATION": ["depreciation and amortisation", "depreciation and amortization", "depreciation"],
    "INTEREST_PAID": ["interest paid", "finance costs", "finance cost", "interest expense"],
    "PRINCIPAL_PAID": ["principal repaid", "principal paid", "repayment of borrowings", "principal repayment"],
    "EMI_AMOUNT": ["emi amount", "emi", "instalment amount", "installment amount"],
    "PAT": ["profit after tax", "net profit", "pat"],
    "TOTAL_DEBT": ["total debt", "total borrowings"],
    "ST_DEBT": ["short-term borrowings", "short term borrowings", "short-term debt", "short term debt"],
    "LT_DEBT": ["long-term borrowings", "long term borrowings", "long-term debt", "long term debt"],
    "NET_DEBT": ["net debt"],
    "CASH": ["cash and cash equivalents", "cash and bank balances", "cash and equivalents", "cash"],
    "NET_WORTH": ["total equity", "net worth", "shareholders funds", "shareholders' funds", "shareholder's fund",
                  "shareholders' fund", "shareholders fund"],
    "TANGIBLE_NET_WORTH": ["tangible net worth"],
    "CURRENT_ASSETS": ["total current assets", "current assets"],
    "CURRENT_LIABILITIES": ["total current liabilities", "current liabilities"],
    "INVENTORY": ["inventories", "inventory"],
    "RECEIVABLES": ["trade receivables", "receivables", "debtors"],
    "CAPEX": ["capital expenditure", "capex", "purchase of property, plant and equipment"],
    # C6: the rest of a full spread (balance sheet, profit and loss, cash flow).
    "OTHER_INCOME": ["other income"],
    "TOTAL_INCOME": ["total income"],
    "MATERIAL_COST": ["cost of materials consumed", "cost of goods sold", "cost of sales", "raw materials consumed"],
    "EMPLOYEE_COST": ["employee benefits expense", "employee benefit expense", "employee benefits", "employee costs",
                      "staff costs"],
    "OTHER_EXPENSES": ["other expenses"],
    "TOTAL_EXPENSES": ["total expenses"],
    "PBT": ["profit before tax", "profit before income tax", "pbt"],
    "TAX": ["total tax expense", "tax expense", "income tax expense", "provision for tax"],
    "FIXED_ASSETS": ["property, plant and equipment", "property plant and equipment", "net fixed assets",
                     "fixed assets"],
    "CWIP": ["capital work-in-progress", "capital work in progress", "cwip"],
    "INTANGIBLES": ["intangible assets", "goodwill and intangible assets"],
    "INVESTMENTS": ["non-current investments", "investments"],
    "TOTAL_ASSETS": ["total assets"],
    "SHARE_CAPITAL": ["equity share capital", "share capital", "stated capital"],
    "RESERVES": ["other equity", "reserves and surplus"],
    "TRADE_PAYABLES": ["trade payables", "creditors"],
    "PROVISIONS": ["provisions"],
    "LEASE_LIABILITIES": ["lease liabilities"],
    "TOTAL_EQUITY_AND_LIABILITIES": ["total equity and liabilities", "total liabilities and equity",
                                     "total equity & liabilities", "total liabilities & equity"],
    "CFO": ["net cash from operating activities", "net cash generated from operating activities",
            "net cash flow from operating activities", "cash generated from operations"],
    "CFI": ["net cash used in investing activities", "net cash from investing activities",
            "net cash flow from investing activities"],
    "CFF": ["net cash used in financing activities", "net cash from financing activities",
            "net cash flow from financing activities"],
    "DIVIDENDS_PAID": ["dividends paid", "dividend paid"],
}

# Wording from real US 10-K and Sri Lankan statements (outputs/benchmarks/statement-pdf-readers/real.md).
for _key, _phrases in {
    "REVENUE": [
        "net revenues",
        "net revenue",
        "total revenues",
        "total sales and revenues",
        "sales and revenues",
    ],
    "PAT": [
        "net earnings",
        "net income",
        "net (loss) income",
        "net income (loss)",
        "net (loss) earnings",
        "net earnings (loss)",
        "profit for the year",
        "profit for the period",
    ],
    "PBT": [
        "earnings before income taxes",
        "income before income taxes",
        "(loss) earnings before income taxes",
        "income (loss) before income taxes",
        "profit before income tax expense",
        "income before taxes",
    ],
    "TAX": [
        "provision for income taxes",
        "income tax (expense)",
        "income tax expense/(reversal)",
    ],
    "INTEREST_PAID": [
        "interest expense, net",
        "finance expenses",
        "finance expense",
    ],
    "DEPRECIATION": [
        "depreciation, depletion and amortization",
        "depreciation and amortisation expense",
    ],
    "RECEIVABLES": [
        "accounts receivable",
        "trade and other receivables",
    ],
    "NET_WORTH": [
        "total stockholders' equity",
        "total shareholders' equity",
        "stockholders' equity",
        "shareholders' equity",
        "total equity attributable to equity holders of the company",
        "equity attributable to owners of the company",
        "equity attributable to equity holders of the parent",
        "total equity attributable to equity holders",
    ],
    "LT_DEBT": [
        "long-term debt, less current portion",
        "long-term debt, net",
    ],
    "TRADE_PAYABLES": [
        "accounts payable",
        "trade and other payables",
    ],
    "CFO": [
        "net cash provided by operating activities",
        "net cash provided by (used in) operating activities",
        "net cash (used in) provided by operating activities",
        "net cash generated from/(used in) operating activities",
        "net cash flows from operating activities",
    ],
    "CFI": [
        "net cash provided by (used in) investing activities",
        "net cash (used in) provided by investing activities",
        "net cash flows used in investing activities",
        "net cash flows from investing activities",
    ],
    "CFF": [
        "net cash provided by (used in) financing activities",
        "net cash (used in) provided by financing activities",
        "net cash flows used in financing activities",
        "net cash flows from financing activities",
    ],
    "CAPEX": [
        "capital expenditures",
        "purchases of property, plant and equipment",
        "purchase of property, plant & equipment",
        "acquisition of property, plant and equipment",
        "additions to properties",
        "additions to property, plant and equipment",
        "purchase of property and equipment",
        "purchases of property and equipment",
        "purchases of property, plant, and equipment",
    ],
}.items():
    LINE_ALIASES[_key] = LINE_ALIASES[_key] + [x for x in _phrases if x not in LINE_ALIASES[_key]]
# Statement wording stress test (8 November 2026): Indian, Sri Lankan, UK and US wordings the catalog lacked.
for _key, _phrases in {
    "REVENUE": ["income from operations", "revenue from contracts with customers", "operating revenue",
                "sales revenue", "gross revenue"],
    "PBT": ["loss before tax", "loss before income tax", "loss before taxation", "profit before taxation"],
    "PAT": ["loss for the year", "loss for the period", "profit after taxation", "net loss", "loss after tax",
            "profit for the year", "profit for the period"],
    "INTEREST_PAID": ["finance charges", "interest and finance charges", "borrowing costs", "financial expenses",
                      "finance expenses", "interest on borrowings"],
    "LT_DEBT": ["non-current borrowings", "non current borrowings", "borrowings (non-current)",
                "borrowings - non-current", "borrowings non-current"],
    "ST_DEBT": ["current borrowings", "borrowings (current)", "borrowings - current"],
    "INVENTORY": ["stock-in-trade", "stock in trade", "stocks"],
    "TAX": ["provision for income taxes", "income tax"],
    "CAPEX": ["payments for property, plant and equipment", "purchases of property and equipment",
              "acquisition of property, plant and equipment", "additions to property, plant and equipment",
              "purchase of property plant and equipment"],
}.items():
    LINE_ALIASES[_key] = LINE_ALIASES[_key] + [x for x in _phrases if x not in LINE_ALIASES[_key]]
# Decorations around a label: "(refer note 12)", a trailing note number "14", a colon.
_DECORATION = re.compile(r"\(\s*(?:refer\s+)?notes?\s*[\w.,\s]*\)|\s+notes?\s+\d[\w.]*\s*$|[:\-–]\s*$|\s+\d{1,3}\s*$",
                         re.I)

# C6: which statement each standard line belongs to, in the order a spread shows them.
SECTIONS: dict[str, list[str]] = {
    "PROFIT_AND_LOSS": ["REVENUE", "OTHER_INCOME", "TOTAL_INCOME", "MATERIAL_COST", "EMPLOYEE_COST",
                        "OTHER_EXPENSES", "TOTAL_EXPENSES", "EBITDA", "DEPRECIATION", "EBIT", "INTEREST_PAID", "PBT",
                        "TAX", "PAT"],
    "BALANCE_SHEET": ["FIXED_ASSETS", "CWIP", "INTANGIBLES", "INVESTMENTS", "INVENTORY", "RECEIVABLES", "CASH",
                      "CURRENT_ASSETS", "TOTAL_ASSETS", "SHARE_CAPITAL", "RESERVES", "NET_WORTH",
                      "TANGIBLE_NET_WORTH", "LT_DEBT", "ST_DEBT", "TOTAL_DEBT", "NET_DEBT", "LEASE_LIABILITIES",
                      "TRADE_PAYABLES", "PROVISIONS", "CURRENT_LIABILITIES", "TOTAL_EQUITY_AND_LIABILITIES"],
    "CASH_FLOW": ["CFO", "CAPEX", "CFI", "PRINCIPAL_PAID", "EMI_AMOUNT", "DIVIDENDS_PAID", "CFF"],
    # 12-month covenants: what 12-month covenants are tested on, built from this period and the comparatives.
    "LAST_12_MONTHS": ["REVENUE_TTM", "EBITDA_TTM", "PBT_TTM", "PAT_TTM", "INTEREST_PAID_TTM", "DEPRECIATION_TTM",
                       "PRINCIPAL_PAID_TTM", "CFO_TTM"],
}
# Lines that contain an alias but mean something else.
_EXCLUDE = re.compile(r"\b(ratio|margin|%|per\s+share|growth|cover|impairment|changes?\s+in|increase|decrease)\b",
                      re.I)

TO_CRORE = {"crore": 1.0, "lakh": 0.01, "million": 0.1, "billion": 100.0, "thousand": 0.0001, "hundred": 1e-5,
            "rupee": 1e-7}
# The rupee sign often comes out of a PDF as a stray character: "All amounts are in ` Lakhs".
# Money for any country (10 November 2026): "Amounts in USD thousands", "AED '000", "PKR in million".
_CODES = CODE_WORDS + r"|us\$|£|€|¥|slrs?"
_UNIT_HINT = re.compile(r"(?:in|amounts?\s+in|₹|rs\.?|inr|lkr|rupees|\$|" + _CODES + r")\s*(?:[`´₹]\s*|I\s+)?(?:in\s+)?"
                        r"(crores?|cr\b|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|thousands?|hundreds?|"
                        r"'?000(?![\d,])|rupees?)"
                        r"|\b(millions?|thousands?)\s+of\s+(?:u\.?s\.?\s+)?(?:dollars|rupees)"
                        # Phase 2: "Amounts in KShs million": any currency word a country profile adds.
                        r"|amounts?\s+in\s+[A-Za-z$.]{2,12}\s+(millions?|thousands?|billions?|'?000(?![\d,]))", re.I)
# "Amounts in INR" / "in rupees" with no scale word after it: plain rupees (golden set: such a statement
# was read as crore, every amount 10^7 too large).
_RUPEE_HINT = re.compile(r"(?:amounts?\s+in|figures?\s+in|in)\s+(?:inr|rs\.?|₹|rupees?|" + _CODES
                         + r")\b(?!\s*(?:in\s+)?(?:crores?|cr\b|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|"
                         r"thousands?|hundreds?|'?000))", re.I)
# A column headed just "Rs." or "LKR" with no scale word: plain rupees (Sri Lankan annual reports).
_CURRENCY_ONLY = re.compile(r"(?<![a-z])(?:rs\.|lkr|inr|usd|pkr|npr|bdt|aed|sar|qar|gbp|eur|sgd)(?![a-z])", re.I)
_INLINE_CRORE = re.compile(r"\d\s*(?:cr|crores?)\b", re.I)
_NUMBER = re.compile(r"\(?-?[0-9][0-9,]*(?:\.[0-9]+)?\)?")
REQUIRED = ("EBITDA", "TOTAL_DEBT", "NET_WORTH")
_PLAIN_EQUITY_TOTAL = re.compile(r"total\s+(?:(?:stockholders|shareholders)['’]?\s+)?equity\b(?!\s+and)", re.I)
# Amounts the catalog keeps as positive figures although a cash-flow statement prints them as outflows.
_OUTFLOWS = frozenset({"CAPEX", "PRINCIPAL_PAID", "DIVIDENDS_PAID"})
# Expense lines are kept as positive amounts although many statements print them in brackets "(8,910)".
# Tax is not among them: a negative tax line is a credit.
_EXPENSES = frozenset({"INTEREST_PAID", "DEPRECIATION", "MATERIAL_COST", "EMPLOYEE_COST", "OTHER_EXPENSES",
                       "TOTAL_EXPENSES"})


@dataclass
class LineValue:
    key: str
    value: float  # INR crore
    unit: str
    derived: bool = False
    page: int | None = None
    cell: str | None = None
    label: str = ""
    months: int | None = None  # the period a profit or cash-flow figure covers (3, 6, 9, 12); None for a balance

    def as_dict(self) -> dict:
        return asdict(self)


# Lines that cover a period (profit and loss, cash flow) rather than stand at a date: a quarter's figure is not a
# year's. TTM_KEYS are the ones a twelve-month figure is built for (12-month covenants, 10 November 2026).
FLOW_KEYS = frozenset({"REVENUE", "OTHER_INCOME", "TOTAL_INCOME", "MATERIAL_COST", "EMPLOYEE_COST", "OTHER_EXPENSES",
                       "TOTAL_EXPENSES", "EBITDA", "EBIT", "DEPRECIATION", "INTEREST_PAID", "PBT", "TAX", "PAT",
                       "PRINCIPAL_PAID", "EMI_AMOUNT", "CAPEX", "CFO", "CFI", "CFF", "DIVIDENDS_PAID"})
TTM_KEYS = ("REVENUE", "EBITDA", "EBIT", "PBT", "PAT", "INTEREST_PAID", "DEPRECIATION", "PRINCIPAL_PAID",
            "EMI_AMOUNT", "CFO", "CAPEX")


@dataclass
class StatementResult:
    values: list[LineValue] = field(default_factory=list)
    unit_scale: str = "crore"
    issues: list[str] = field(default_factory=list)
    money: str = "INR_CR"  # the money unit the values are in (dps.money): INR crore, or millions of the currency


def _quotes(text: str) -> str:
    # "Rs.’000": a typographic apostrophe is still the thousands mark
    return (text or "").replace("’", "'").replace("‘", "'")


def _unit(text: str) -> str:
    """The scale the statement states; "crore" when it states none (see :func:`unit_stated`)."""
    text = _quotes(text)
    m = _UNIT_HINT.search(text)
    if not m:
        if _INLINE_CRORE.search(text or ""):
            return "crore"  # "Rs. 1,234.5 Cr" on every value
        return "rupee" if _RUPEE_HINT.search(text or "") or _CURRENCY_ONLY.search(text or "") else "crore"
    word = next(g for g in m.groups() if g).lower()
    if word.startswith(("cr",)):
        return "crore"
    if word.startswith(("lakh", "lac")):
        return "lakh"
    if word.startswith(("mill", "mn")):
        return "million"
    if word.startswith(("bill", "bn")):
        return "billion"
    if word.startswith(("thous", "'000", "000")):
        return "thousand"
    if word.startswith("hundred"):
        return "hundred"  # held-out set: "Rs. In Hundreds" was read as plain rupees, every amount 100 times too small
    return "rupee"


def unit_stated(text: str) -> bool:
    text = _quotes(text)
    return bool(_UNIT_HINT.search(text or "") or _RUPEE_HINT.search(text or "") or _CURRENCY_ONLY.search(text or "")
                or _INLINE_CRORE.search(text or ""))


_NUMBERING = re.compile(r"^\s*(?:\(?[a-z0-9]{1,4}[.)]\s*)+")
# Lines read as totals only when the label says so (or is exactly the phrase).
_TOTALS = frozenset({"CURRENT_ASSETS", "CURRENT_LIABILITIES", "TOTAL_ASSETS"})
# Families worded in too many ways for a phrase list: "Net Cash Flows Provided by (Used in) Operating
# Activities", "Income of consolidated group before income taxes".
_CASH_FROM = r"(\bnet\s+cash\b|^\s*cash\s+(provided|used|generated|from)\b)"
_PATTERNS = [
    # "Net cash provided by ...", and US filings' "Cash provided by (used in) operating activities"
    ("CFO", re.compile(_CASH_FROM + r".{0,60}\boperating\s+activities\b")),
    ("CFO", re.compile(r"^\s*(net\s+)?cash\s+(provided|generated)\s+(by|from)\s+operations\s*$")),
    ("CFI", re.compile(_CASH_FROM + r".{0,60}\binvesting\s+activities\b")),
    ("CFF", re.compile(_CASH_FROM + r".{0,60}\bfinancing\s+activities\b")),
    # US income statements: "Income taxes", "Income tax provision", "Provision (benefit) for income taxes"
    ("TAX", re.compile(r"^\s*income\s+tax(es)?\s*$|^\s*income\s+tax\s+provision\b|^\s*provision\s+\(benefit\)\s+for\s+"
                       r"income\s+tax")),
    ("REVENUE", re.compile(r"^\s*sales\s*$")),
    ("PBT", re.compile(r"\b(profit|income|earnings)\b.{0,50}\bbefore\s+(income\s+)?tax(es|ation)?\b")),
]


def _plain(text: str) -> str:
    # "Trade & Other Payables" is "trade and other payables"; "Profit/(Loss) for the year" is "profit for the year"
    text = text.lower().replace("’", "'").replace("&", " and ")
    text = re.sub(r"\s*/\s*\((loss|profit)\)|\((loss|profit)\)\s*/\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def match_key(label: str) -> str | None:
    text = " " + _plain(_DECORATION.sub(" ", _DECORATION.sub(" ", label or ""))) + " "
    if _EXCLUDE.search(text):
        return None
    lead = _NUMBERING.sub("", text.strip())
    best, best_phrase = None, ""
    for key, phrases in LINE_ALIASES.items():
        for phrase in map(_plain, phrases):
            # "non-current assets" is not "current assets": a hyphen before the phrase is no word boundary.
            if re.search(r"(?<![a-z-])" + re.escape(phrase) + r"(?![a-z])", text) and len(phrase) > len(best_phrase):
                best, best_phrase = key, phrase
    if best and re.search(r"\bnon[- ]current\b", text) and "non" not in best_phrase:
        return None  # "Total Non Current Assets" is not total current assets
    if best and lead.startswith("other ") and not best_phrase.startswith("other"):
        return None  # "Other current assets" is one line inside current assets, not the total
    if "exceptional" in text and (best == "PBT" or re.search(r"before\s+(income\s+)?tax", text)):
        return None  # "Profit before exceptional items and tax" is not the profit before tax
    for key, rx in _PATTERNS:
        if rx.search(text) or rx.search(lead):  # "(ii) Sales", "3. Income taxes": the numbering is no part of it
            return key
    if best in _TOTALS and not (lead.startswith("total") or lead.strip(" :") == best_phrase
                                or re.search(r",\s*total\s*$", lead)):  # "Current assets, total"
        return None  # "Prepaid expenses and other current assets" is not total current assets
    if best == "LT_DEBT" and re.search(r"current\s+(portion|maturities)|due\s+within", text) \
            and not re.search(r"\b(less|excluding|net\s+of)\s+(the\s+)?current\s+(portion|maturities)", text):
        return None  # the part of long-term debt due within a year is not long-term debt
    # Statement wording stress test: a cash-flow line is never a balance, a deferred or grant revenue never the sales.
    if best == "CASH" and re.search(r"\b(provided|generated|used|inflow|outflow|flows?|increase|decrease)\b", text):
        return None
    if best == "FIXED_ASSETS" and re.search(r"\b(purchases?|payments?|acquisition|additions?|proceeds|sale|disposal)\b",
                                            text):
        return None
    if best == "TAX" and re.search(r"\b(payable|receivables?|recoverable|refunds?|assets?|liabilit\w*|deferred|paid|"
                                   r"advances?)\b", text):
        return None  # bug hunt (held-out set): "Income tax payable", "Deferred income tax asset" are not the tax charge
    if best == "REVENUE" and re.search(r"\b(deferred|unearned|grants?|cost\s+of|reserves?)\b", text):
        return None  # Sri Lanka run: "Revenue reserves" is retained earnings in equity, not the sales
    if best == "INVENTORY" and re.search(r"\b(provisions?|reversal|write[- ]?(?:offs?|downs?)|obsolete|slow[- ]moving|"
                                         r"impairment|allowance)\b", text):
        return None  # Sri Lanka run (Hemas): "Provision for Obsolete & Slow-Moving Stocks" in the cash flow
    if best == "CASH" and re.search(r"\bbeginning\s+of\b|\bopening\b", text):
        return None  # held-out set: "Cash ... as at the beginning of the year" is last year's closing cash
    if best in ("PAT", "PBT") and re.search(r"\bshare\s+of\b|non-?controlling|minority", text):
        return None  # held-out set: "Share of net income of equity companies" is not the company's net income
    if best in ("LT_DEBT", "ST_DEBT", "TOTAL_DEBT") and re.search(r"\b(proceeds|repayments?|repaid|raised)\b", text):
        return None  # held-out set: "Proceeds from long term borrowings" is a cash flow, not the debt
    return best


# Bug hunt (8 November 2026): a spreadsheet cell "−50" (the minus sign U+2212), "$1,234", "₹ 1,234" or "1 234" was
# not read, so the line was lost.
_CURRENCY_MARK = re.compile(r"^(?:US\$|\$|₹|INR|Rs\.?|LKR|USD)\s*", re.I)
_SPACED_THOUSANDS = re.compile(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:\.\d+)?")


# Number style (Phase 2, 11 November 2026). India "1,50,000.00" and the UK or US "150,000.00" use the comma for
# thousands; much of Europe and Latin America writes "150.000,00". The style is set per document while it is read
# (number_style, detect_number_style); the default is the comma style, as before.
_STYLE: contextvars.ContextVar[str] = contextvars.ContextVar("number_style", default="WESTERN")
_EUROPEAN_TOKEN = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:\.\d{3})+,\d{1,3}|\d{1,3}(?:\.\d{3}){2,}|\d+,\d{2})(?![\d.,])")
_COMMA_TOKEN = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{2,3})+\.\d{1,3}|\d{1,3}(?:,\d{2,3}){2,}|\d+\.\d{2})(?![\d.,])")


@contextlib.contextmanager
def number_style(style: str):
    """Read numbers in ``style`` ("EUROPEAN": "1.234,5"; anything else: "1,234.5") inside the block."""
    token = _STYLE.set("EUROPEAN" if style == "EUROPEAN" else "WESTERN")
    try:
        yield
    finally:
        _STYLE.reset(token)


def detect_number_style(text: str) -> str | None:
    """The style a document's numbers are written in: "EUROPEAN", "WESTERN" (India's too), "UNCLEAR" when it uses
    both alike, or None when nothing tells (whole numbers under 1,000, or "1.234" alone)."""
    euro = len(_EUROPEAN_TOKEN.findall(text or ""))
    comma = len(_COMMA_TOKEN.findall(text or ""))
    if euro and comma:
        if euro >= 10 * comma:
            return "EUROPEAN"
        return "WESTERN" if comma >= 10 * euro else "UNCLEAR"
    return "EUROPEAN" if euro else "WESTERN" if comma else None


def european_to_comma(text: str) -> str:
    """"1.234.567,89" -> "1,234,567.89" for each number of the text, when the document's style is European."""
    if _STYLE.get() != "EUROPEAN":
        return text
    return re.sub(r"\d[\d.,]*\d|\d", lambda m: m.group(0).translate(str.maketrans(".,", ",.")), text)


def parse_number(raw: str) -> float | None:
    return _parse_comma(european_to_comma((raw or "").strip()))


def _parse_comma(raw: str) -> float | None:
    s = raw.replace("\u2212", "-")
    if not s or s in ("-", "\u2014", "\u2013"):
        return None
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-")
    digits = _CURRENCY_MARK.sub("", s.strip("()").lstrip("-").strip())
    if _SPACED_THOUSANDS.fullmatch(digits):
        digits = re.sub(r"[ \u00a0\u202f]", "", digits)
    digits = digits.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def rows_from_text(pages: list[tuple[int, str]]) -> list[tuple[str, float, int | None, str | None]]:
    """(label, first number on the line, page, None) for each 'label .... number' line."""
    out = []
    for page_no, text in pages:
        for line in (text or "").splitlines():
            line = european_to_comma(line)
            first = _NUMBER.search(line)
            if not first or first.start() == 0:
                continue
            label = line[:first.start()].strip(" .:-\t")
            if re.search(r"\d", label[-1:] or ""):
                continue
            value = _parse_comma(first.group(0))  # the line is in the comma style already
            if label and value is not None:
                out.append((label, value, page_no, None))
    return out


def _cell_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return parse_number(v) if isinstance(v, str) else None


def _year_like(v) -> bool:
    n = _cell_number(v)
    return n is not None and n.is_integer() and 1990 <= n <= 2100 and not (isinstance(v, str) and "," in v)


def rows_from_grid(grid: list[list], ref,
                   entity: str = "STANDALONE") -> list[tuple[str, float, int | None, str | None]]:
    """Label and value of each row of one sheet, the value from the column a statement reader should use.

    Statement stress test (8 November 2026): taking the first number after the label read the "Note" column
    ("Revenue | 23 | 5,000 | 4,500" gave 23) and, where the earlier year is printed first, last year's figures. The
    column is chosen like the PDF reader's (statement_pdf.choose_column): headed by the latest year, of the bank's
    entity, never a note column. ``ref(row, col)`` gives the cell reference shown to the reviewer.
    """
    from dps.extraction.statement_pdf import _DASHES, _is_note_column, choose_column, other_period

    parsed = []  # (row index, label, {col: number}, header-like)
    for i, row in enumerate(grid):
        label, label_col = None, None
        for j, v in enumerate(row):
            if isinstance(v, str) and v.strip() and _cell_number(v) is None:
                label, label_col = v.strip(), j
                break
        numbers = {j: _cell_number(v) for j, v in enumerate(row)
                   if (label_col is None or j > label_col) and _cell_number(v) is not None}
        # "Particulars | 2025 | 2024" heads the columns; "Total equity | 2000" is a figure that looks like a year.
        header = not numbers or (all(_year_like(row[j]) for j in numbers) and (
            label is None or (match_key(label) is None and (len(numbers) >= 2 or re.search(
                r"particulars|year|as\s+at|ended|period|note|description|amounts?\s+in", label, re.I)))))
        parsed.append((i, label, numbers, header))
    first_data = next((k for k, (_, label, numbers, header) in enumerate(parsed) if label and not header),
                      len(parsed))
    headers: dict[int, str] = {}
    for i, _, _, _header in parsed[:first_data] + [p for p in parsed[first_data:] if p[3] and not p[1]]:
        for j, v in enumerate(grid[i]):
            if v is not None and str(v).strip():
                headers[j] = (headers.get(j, "") + " " + str(v).strip()).strip()
    values: dict[int, list[float]] = {}
    for _, label, numbers, header in parsed:
        if label and not header:
            for j, n in numbers.items():
                values.setdefault(j, []).append(n)
    best = choose_column(headers, values, entity) if values else None
    notes = {j for j, vals in values.items() if _is_note_column(headers.get(j, ""), vals)}
    out = []
    for i, label, numbers, header in parsed:
        if not label or header or not numbers:
            continue
        if best in numbers:
            col = best
        elif best is not None and best < len(grid[i]) and str(grid[i][best] or "").strip().lower() in _DASHES:
            out.append((label, 0.0, None, ref(i, best)))  # "-" in this year's column: nil, not last year's figure
            continue
        else:
            col = next((j for j in sorted(numbers) if j not in notes and not other_period(headers, best, j)), None)
        if col is not None:
            out.append((label, numbers[col], None, ref(i, col)))
    return out


def rows_from_xlsx(data: bytes, entity: str = "STANDALONE") -> list[tuple[str, float, int | None, str | None]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        out += rows_from_grid(grid, lambda i, j, t=ws.title: f"{t}!{_column(j)}{i + 1}", entity)
    return out


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (spreadsheet column letters for cell references)."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _xls_book(data: bytes):
    import xlrd

    try:
        return xlrd.open_workbook(file_contents=data)
    except xlrd.XLRDError as ex:  # not a real .xls (or an encrypted one): a clear reason for the reviewer
        raise ParseError(f"Unreadable .xls statement: {ex}") from ex


def rows_from_xls(data: bytes, entity: str = "STANDALONE") -> list[tuple[str, float, int | None, str | None]]:
    """FR-01: the older Excel format (.xls), read like .xlsx."""
    book = _xls_book(data)
    out = []
    for sheet in book.sheets():
        grid = [[None if v == "" else v for v in sheet.row_values(r)] for r in range(sheet.nrows)]
        out += rows_from_grid(grid, lambda i, j, t=sheet.name: f"{t}!{_column(j)}{i + 1}", entity)
    return out


def header_from_xls(data: bytes, max_rows: int = 10) -> str:
    book = _xls_book(data)
    return " ".join(str(v) for s in book.sheets() for r in range(min(max_rows, s.nrows))
                    for v in s.row_values(r) if isinstance(v, str) and v.strip())


def header_from_xlsx(data: bytes, max_rows: int = 10) -> str:
    """Text of the first rows of each sheet, where a unit note ("Amounts in INR lakhs") usually sits."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(max_row=max_rows, values_only=True):
            parts.extend(str(v) for v in row if isinstance(v, str))
    return " ".join(parts)


def header_from_csv(data: bytes, max_lines: int = 10) -> str:
    return " ".join(data.decode("utf-8-sig", errors="replace").splitlines()[:max_lines])


def rows_from_csv(data: bytes, entity: str = "STANDALONE") -> list[tuple[str, float, int | None, str | None]]:
    text = data.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    if lines and sum(";" in line for line in lines) >= max(2, len(lines) // 2):
        # Phase 2: a European export ("EBITDA;1.234,5") under a title line: the comma is the decimal mark there.
        dialect = type("semicolon", (csv.excel,), {"delimiter": ";"})
    else:
        try:  # bug hunt (8 November 2026): a semicolon or tab separated export read no rows at all
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
    grid = [[c if c.strip() else None for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    return rows_from_grid(grid, lambda i, j: f"R{i + 1}C{j + 1}", entity)


def extract_values(rows: list[tuple[str, float, int | None, str | None]], header_text: str,
                   currency: str = "INR", rate: float = 1.0) -> StatementResult:
    """Values in ``currency``'s money unit (INR crore, millions of any other currency, dps.money); ``rate`` converts
    a statement printed in another currency into it (money for any country, 10 November 2026)."""
    from dps.money import money_unit, to_money_unit

    scale = _unit(header_text)
    if currency != "INR" and not unit_stated(header_text):
        scale = "million"  # crore is an Indian scale: elsewhere statements are kept in millions (held, see below)
    factor = to_money_unit(1.0, scale, currency, rate)
    money = money_unit(currency)
    found: dict[str, LineValue] = {}
    dues: list[float] = []
    expense_signs: list[float] = []
    for label, value, page, cell in rows:
        key = match_key(label)
        lead = _NUMBERING.sub("", label.lower().strip())
        if key == "REVENUE" and lead.startswith("total") and "OTHER_INCOME" in found:
            key = "TOTAL_INCOME"  # Indian "Total revenue" after other income is the total income
        if key == "NET_WORTH" and key in found and "attributable" in found[key].label.lower() \
                and "attributable" not in lead:
            del found[key]  # with non-controlling interests the total equity is the net worth
        if key == "NET_WORTH" and key in found and _PLAIN_EQUITY_TOTAL.match(lead) \
                and not _PLAIN_EQUITY_TOTAL.match(_NUMBERING.sub("", found[key].label.lower().strip())):
            del found[key]  # held-out set: "Total Kimberly-Clark Corporation Stockholders' Equity" is the parent's part
        if key == "CFO" and key in found and "cash generated from operations" in found[key].label.lower() \
                and lead.startswith("net cash"):
            del found[key]  # held-out set: cash generated from operations is before interest, tax and gratuity paid
        if key in ("REVENUE", "TAX") and key in found and lead.startswith("total") \
                and not _NUMBERING.sub("", found[key].label.lower()).startswith("total"):
            del found[key]  # "Net sales" then "Total net sales and revenues": the total is the revenue
        if key in _EXPENSES and key not in found or re.search(r"\b(cost of sales|administrative expenses|"
                                                             r"distribution expenses)\b", lead):
            expense_signs.append(value)
        if "outstanding dues" in lead and len(dues) < 2:
            dues.append(value)  # trade payables split into micro / small enterprises and the rest
            if len(dues) == 2:
                found.pop("TRADE_PAYABLES", None)
                key, value, label = "TRADE_PAYABLES", sum(dues), "trade payables (micro and small + others)"
            elif key == "TRADE_PAYABLES":
                key = None
        if key and key not in found:  # first occurrence = current period / primary statement
            if key in _OUTFLOWS or key in _EXPENSES:
                value = abs(value)  # "Purchase of property, plant and equipment (135.88)" is capex of 135.88
            found[key] = LineValue(key, round(value * factor, 6), money, False, page, cell, label[:120])
    if "TAX" in found and sum(1 for v in expense_signs if v < 0) > len(expense_signs) / 2:
        # Expenses printed in brackets ("Figures in brackets indicate deductions"): "(316,523)" is a tax
        # charge and a bare "8,993" a tax credit. The catalog keeps a charge positive.
        found["TAX"].value = -found["TAX"].value
    result = StatementResult(list(found.values()), scale, money=money)
    derive(result)
    check(result)
    if found and not unit_stated(header_text):
        # Assuming crore when nothing says so must not pass silently: a person confirms the scale.
        result.issues.append(f"UNIT_NOT_STATED: amounts read as {'INR crore' if currency == 'INR' else currency
                                                                    + ' million'}")
    return result


def _put(result: StatementResult, key: str, value: float, *inputs: str) -> None:
    result.values.append(LineValue(key, round(value, 6), result.money, True,
                                   label="derived from " + " + ".join(inputs)))


def derive(result: StatementResult) -> None:
    """Calculate step: fill missing inputs from the ones reported."""
    v = {x.key: x.value for x in result.values}
    parts = [k for k in ("ST_DEBT", "LT_DEBT") if k in v]
    if "TOTAL_DEBT" not in v and parts:  # a statement with no short-term line has none to add
        v["TOTAL_DEBT"] = sum(v[k] for k in parts)
        _put(result, "TOTAL_DEBT", v["TOTAL_DEBT"], *parts)
    if "EBITDA" not in v and "EBIT" in v and "DEPRECIATION" in v:
        v["EBITDA"] = v["EBIT"] + abs(v["DEPRECIATION"])
        _put(result, "EBITDA", v["EBITDA"], "EBIT", "DEPRECIATION")
    if "EBITDA" not in v and {"PBT", "INTEREST_PAID", "DEPRECIATION"} <= v.keys():
        # Sri Lanka run: no Sri Lankan income statement prints EBITDA, so all 25 were held for it. The usual build:
        # profit before tax, plus finance costs, plus depreciation and amortisation (the cash flow add-back).
        v["EBITDA"] = v["PBT"] + abs(v["INTEREST_PAID"]) + abs(v["DEPRECIATION"])
        _put(result, "EBITDA", v["EBITDA"], "PBT", "INTEREST_PAID", "DEPRECIATION")
    if "NET_DEBT" not in v and "TOTAL_DEBT" in v and "CASH" in v:
        _put(result, "NET_DEBT", v["TOTAL_DEBT"] - v["CASH"], "TOTAL_DEBT", "-CASH")
    if "EMI_AMOUNT" not in v and "INTEREST_PAID" in v and "PRINCIPAL_PAID" in v:
        _put(result, "EMI_AMOUNT", abs(v["INTEREST_PAID"]) + abs(v["PRINCIPAL_PAID"]), "INTEREST_PAID",
             "PRINCIPAL_PAID")


def check(result: StatementResult) -> None:
    """Validate step data-quality rules (03 §3)."""
    v = {x.key: x.value for x in result.values}
    missing = [k for k in REQUIRED if k not in v]
    if missing:
        result.issues.append("MISSING_LINE_ITEMS: " + ", ".join(missing))
    for key in ("TOTAL_DEBT", "ST_DEBT", "LT_DEBT", "CASH", "CURRENT_ASSETS", "CURRENT_LIABILITIES", "INVENTORY",
                "RECEIVABLES"):
        if v.get(key, 0) < 0:
            result.issues.append(f"NEGATIVE_VALUE: {key}")
    if {"TOTAL_DEBT", "ST_DEBT", "LT_DEBT"} <= v.keys():
        parts = v["ST_DEBT"] + v["LT_DEBT"]
        if abs(parts - v["TOTAL_DEBT"]) > max(0.01, 0.01 * abs(v["TOTAL_DEBT"])):
            result.issues.append(f"DEBT_DOES_NOT_ADD_UP: {v['TOTAL_DEBT']} vs {round(parts, 6)}")
    if "EBITDA" in v and "EBIT" in v and v["EBIT"] > v["EBITDA"] + 1e-9:
        result.issues.append("EBIT_ABOVE_EBITDA")
    interest = next((x for x in result.values if x.key == "INTEREST_PAID" and not x.derived), None)
    if interest is not None and _NET_INTEREST.search(interest.label.lower()):
        # Held-out set (HU01): "Interest expense, net" is 190.2 where gross interest is 224.8. Which one the
        # covenant uses is the agreement's definition, so a person confirms it once per borrower.
        result.issues.append(f"INTEREST_IS_NET: '{interest.label[:80]}'")
    result.issues.extend(identity_issues(v))


# "Interest expense, net", "Net finance costs", "Finance costs (net)"; not "net of amounts capitalised".
_NET_INTEREST = re.compile(r"\bnet\b(?!\s+of\b)")


# The inputs of covenant formulas, and words a line for each is written with when the catalog does not know the
# exact wording ("Long-term debt and finance lease obligations", "Net cash inflow from trading activities").
COVENANT_INPUTS: dict[str, str] = {
    "REVENUE": r"revenue|sales|turnover|income\s+from\s+operations",
    "PBT": r"before\s+(income\s+)?tax",
    "PAT": r"net\s+(income|earnings|profit)|profit\s+(for|after)|profit\s+attributable",
    "INTEREST_PAID": r"interest|finance\s+(cost|charge|expense)",
    "DEPRECIATION": r"depreciation|amorti[sz]ation",
    "TAX": r"\btax(es|ation)?\b",
    "TOTAL_ASSETS": r"total\s+assets",
    "NET_WORTH": r"equity|net\s+worth|shareholders|stockholders",
    "CURRENT_ASSETS": r"current\s+assets",
    "CURRENT_LIABILITIES": r"current\s+liabilities",
    "LT_DEBT": r"long[- ]term\s+(debt|borrowings|loans)|non[- ]current\s+borrowings|term\s+loans?|debentures|notes\s+"
               r"payable|bonds",
    "ST_DEBT": r"short[- ]term\s+(debt|borrowings|loans)|current\s+borrowings|bank\s+overdraft|commercial\s+paper|"
               r"current\s+portion\s+of\s+(long[- ]term\s+)?debt",
    "CASH": r"\bcash\b",
    "CFO": r"operating\s+activities",
    "INVENTORY": r"inventor|stock[- ]in[- ]trade",
    "RECEIVABLES": r"receivable|debtors",
}


def likely_lines(rows: list[tuple[str, float, int | None, str | None]], found: set[str], factor: float
                 ) -> list[dict]:
    """For each covenant input not found: the unmapped line that most likely means it, for a person to confirm.

    Nothing is stored from it. It turns "Long-term borrowings: not found" into "not found; page 2 has 'Borrowings -
    non-current' 3,069.6: is that it?", so a new wording is not silently missed. A line is ranked by its meaning
    (similarity.nearest, which rewrites both wordings into one financial vocabulary); the word list in
    COVENANT_INPUTS still finds a line the matcher scores low.
    """
    from dps.extraction.similarity import nearest

    best: dict[str, tuple[float, dict]] = {}
    for label, value, page, cell in rows:
        text = _plain(label)
        if match_key(label) is not None or _EXCLUDE.search(f" {text} "):
            continue
        if re.search(r"\b(deferred|accumulated|per\s+share|attributable\s+to\s+non)", text):
            continue
        meant, score = nearest(label)
        for key, words in COVENANT_INPUTS.items():
            if key in found:
                continue
            if key.startswith("CURRENT") and re.search(r"non[- ]?current|^\W*(\w+\W+)?other\b", text):
                continue  # "Other non-current assets" is not current assets
            if key == "TAX" and re.search(r"assets|liabilit|payable|receivable|before\s+tax|\bpaid\b|refund", text):
                continue  # a tax balance, or tax paid in the cash flow, is not the tax charge
            if key in ("TOTAL_ASSETS", "CURRENT_ASSETS", "CURRENT_LIABILITIES") and "total" not in text:
                continue  # "Lease assets" is one line, not total assets
            if key == "RECEIVABLES" and re.search(r"\btax", text):
                continue
            rank = score if meant == key and score >= LIKELY_SCORE else (0.5 if re.search(words, text, re.I) else 0)
            if rank and rank > best.get(key, (0.0, {}))[0]:
                best[key] = (rank, {"key": key, "label": label[:120], "value": round(value * factor, 6),
                                    "page": page, "cell": cell})
    return [best[k][1] for k in COVENANT_INPUTS if k in best]


# A meaning match at least this close names the line (benchmark: section 14).
LIKELY_SCORE = 0.6


def _off(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) > max(0.01, tolerance * max(abs(a), abs(b)))


def identity_issues(v: dict[str, float]) -> list[str]:
    """Accounting identities a correctly read statement satisfies; a failed one means a figure was misread.

    Checked without any answer key: the balance sheet balances, a part is not larger than its total, the profit
    lines and the income lines add up. Each needs both sides read, so a missing line raises nothing here.
    """
    out = []
    if {"TOTAL_ASSETS", "TOTAL_EQUITY_AND_LIABILITIES"} <= v.keys() and _off(v["TOTAL_ASSETS"],
                                                                            v["TOTAL_EQUITY_AND_LIABILITIES"], 0.005):
        out.append(f"BALANCE_SHEET_DOES_NOT_BALANCE: assets {v['TOTAL_ASSETS']} vs equity and liabilities "
                   f"{v['TOTAL_EQUITY_AND_LIABILITIES']}")
    total = v.get("TOTAL_ASSETS") or v.get("TOTAL_EQUITY_AND_LIABILITIES")
    if total and total > 0:
        for key in ("CURRENT_ASSETS", "CURRENT_LIABILITIES", "CASH", "INVENTORY", "RECEIVABLES", "LT_DEBT",
                    "ST_DEBT", "FIXED_ASSETS"):
            if v.get(key, 0) > total * 1.005:
                out.append(f"PART_ABOVE_TOTAL: {key} {v[key]} is more than total assets {total}")
    if {"PBT", "TAX", "PAT"} <= v.keys():
        # Equity-accounted earnings, minority shares and discontinued operations sit between PBT less tax and PAT
        # (6% of PBT on the US filings benchmarked), so only a gap of more than 15% of PBT counts.
        scale = max(abs(v["PBT"]), abs(v.get("REVENUE", 0)) * 0.02, 0.01)
        if abs(v["PBT"] - v["TAX"] - v["PAT"]) > 0.15 * scale + 0.01 and abs(v["PBT"] + v["TAX"] - v["PAT"]) > \
                0.15 * scale + 0.01:
            out.append(f"PROFIT_DOES_NOT_ADD_UP: profit before tax {v['PBT']} less tax {v['TAX']} is not profit "
                       f"after tax {v['PAT']}")
    if {"REVENUE", "OTHER_INCOME", "TOTAL_INCOME"} <= v.keys() and _off(v["REVENUE"] + v["OTHER_INCOME"],
                                                                         v["TOTAL_INCOME"], 0.005):
        out.append(f"INCOME_DOES_NOT_ADD_UP: revenue {v['REVENUE']} plus other income {v['OTHER_INCOME']} is not "
                   f"total income {v['TOTAL_INCOME']}")
    if {"CURRENT_ASSETS", "TOTAL_ASSETS"} <= v.keys() and v["CURRENT_ASSETS"] > 0 and \
            v.get("CASH", 0) + v.get("INVENTORY", 0) + v.get("RECEIVABLES", 0) > v["CURRENT_ASSETS"] * 1.005:
        out.append("CURRENT_ASSETS_TOO_SMALL: cash, inventory and receivables add up to more than current assets")
    return out
