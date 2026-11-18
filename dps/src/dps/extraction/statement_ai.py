"""Flow B Validate step, optional: a second reader for the statement figures (Azure OpenAI), grounded and voting.

The table reader (statement_pdf) is the source of every stored value. When ``DPS_STATEMENT_AI_CHECK`` is on and a
model is configured, the model reads the same statement pages and says, for each covenant input, the number as
printed, the page and the printed line. Then (phase 2 of "never miss silently"):

1. **Grounding** (verify.py): an answer counts only if that number is printed on a line of that page carrying
   the line's words. Anything else (a sum the model did itself, a previous year, a guess) is dropped.
2. **Voting** on each covenant input:
   - both readers read the same figure: **confirmed** (shown to the reviewer as checked twice);
   - they read different figures: ``SECOND_READER_DIFFERS``, the statement waits for a person;
   - only the model found it: offered as a suggestion on the line ("the second reader found ... on page 3"),
     never stored on its own.

The model's numbers are never stored. A model outage gives an empty reading, so statements are never blocked.

Why both: on 47 real reports (outputs/benchmarks/real-documents/) the table reader was right on 95% of the
hand-checked Indian / Sri Lankan values and 89.5% of the SEC XBRL values; the model on 84% and 93%. Their
mistakes fall on different lines, so a value both readers agree on is far safer than either alone.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from dps.ai_guard import DATA_NOT_INSTRUCTIONS
from dps.extraction.statement import _EXPENSES, LINE_ALIASES, TO_CRORE, LineValue
from dps.extraction.verify import number_on_line

log = logging.getLogger(__name__)

# The inputs of the covenant formulas; other lines (provisions, CWIP...) do not change a ratio.
CHECKED = ("REVENUE", "PBT", "PAT", "INTEREST_PAID", "DEPRECIATION", "TOTAL_ASSETS", "NET_WORTH", "CURRENT_ASSETS",
           "CURRENT_LIABILITIES", "LT_DEBT", "ST_DEBT", "CASH", "CFO", "INVENTORY", "RECEIVABLES")
TOLERANCE = 0.005  # half a percent: rounding between "1,234.5" and "1,234.47"
MAX_CHARS = 60000
PAGE_MARK = re.compile(r"^=== Page (\d+) ===$", re.M)

PROMPT = """You read a company's financial statement for a bank's covenant monitoring.
Return JSON only: {{"unit": "crore|lakh|million|thousand|rupee",
 "values": {{KEY: {{"value": number, "page": page number, "line": "the printed line, label and number"}}}}}}.
Use the CURRENT annual period (the latest year ended). {entity} Never a quarter, half-year or
previous year, never a ratio or percentage. Negative numbers in brackets are negative. Give each number
exactly as printed in the stated unit (do not convert, do not add lines up). Copy "line" from the page as it is
printed. Leave out any key the statement does not print on one line. Pages are marked "=== Page N ===".
Keys and what they mean (read exactly this, the bank's definitions): {keys}

{guard}

Statement text:
{text}"""

Completion = Callable[[list[dict]], str]

# What the bank means by each figure, the same choice the table reader makes (statement.py). Held-out set: without
# these the second reader picked the other column or the other definition (group against company, gross against net
# interest, equity with or without minority interests) and every statement waited for a person.
ENTITY = {
    "STANDALONE": "When both standalone (company) and consolidated (group) figures are shown, side by side or on "
                  "separate pages, use the standalone (company) ones; otherwise use the figures given.",
    "CONSOLIDATED": "When both consolidated (group) and standalone (company) figures are shown, side by side or on "
                    "separate pages, use the consolidated (group) ones; otherwise use the figures given.",
}
DEFINITIONS = {
    "REVENUE": "revenue from operations / net sales / total revenue, without other income",
    "PBT": "profit (or loss) before tax",
    "PAT": "profit (or loss) for the year, the company's total, not a share of associates or of minority interests",
    "INTEREST_PAID": "the finance costs / interest expense line of the profit and loss statement as printed (net if "
                     "the statement prints it net), not the cash-flow line",
    "DEPRECIATION": "depreciation and amortisation expense",
    "TOTAL_ASSETS": "total assets",
    "NET_WORTH": "total equity, including non-controlling (minority) interests when the statement prints that total",
    "CURRENT_ASSETS": "total current assets",
    "CURRENT_LIABILITIES": "total current liabilities",
    "LT_DEBT": "long-term (non-current) borrowings or debt, without the current portion",
    "ST_DEBT": "short-term (current) borrowings, including the current portion of long-term debt if printed in it",
    "CASH": "cash and cash equivalents at the END of the year, from the balance sheet",
    "CFO": "net cash from (used in) operating activities, after interest and tax paid",
    "INVENTORY": "inventories",
    "RECEIVABLES": "trade receivables / accounts receivable (current)",
}


@dataclass
class Reading:
    """One answer of the model for one key, after grounding."""
    key: str
    value: float  # in crore, like the table reader's values
    printed: float  # as printed, in the statement's unit
    page: int | None
    line: str
    grounded: bool


@dataclass
class SecondReading:
    issues: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)  # keys both readers read the same
    suggestions: list[dict] = field(default_factory=list)  # keys only the model found, grounded on the page
    dropped: int = 0  # answers not found on the cited page


def split_pages(text: str) -> dict[int, str]:
    """"=== Page 3 ===\\n..." blocks back into {3: text}; text without marks is page 0."""
    marks = list(PAGE_MARK.finditer(text or ""))
    if not marks:
        return {0: text or ""}
    return {int(m.group(1)): text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
            for i, m in enumerate(marks)}


def mark_pages(pages: list[tuple[int, str]]) -> str:
    return "\n\n".join(f"=== Page {no} ===\n{text}" for no, text in pages)[:MAX_CHARS]


def prompt(text: str, entity: str = "STANDALONE") -> str:
    keys = "; ".join(f"{k}: {DEFINITIONS.get(k, LINE_ALIASES[k][0])}" for k in CHECKED)
    return PROMPT.format(entity=ENTITY[entity], keys=keys, text=text[:MAX_CHARS], guard=DATA_NOT_INSTRUCTIONS)


def ai_read(text: str, completion: Completion, entity: str = "STANDALONE") -> list[Reading]:
    """The model's reading of the checked keys, each grounded against the page it cites."""
    answer = completion([{"role": "user", "content": prompt(text, entity)}])
    data = json.loads(answer[answer.find("{"): answer.rfind("}") + 1])
    factor = TO_CRORE.get(str(data.get("unit", "crore")).lower(), 1.0)
    pages = split_pages(text)
    out = []
    for key, item in (data.get("values") or {}).items():
        if key not in CHECKED:
            continue
        if isinstance(item, (int, float)):
            item = {"value": item}  # a bare number cannot be grounded: it is dropped below
        if not isinstance(item, dict) or not isinstance(item.get("value"), (int, float)):
            continue
        printed = float(item["value"])
        page = item.get("page") if isinstance(item.get("page"), int) else None
        line = str(item.get("line") or "")
        label = re.sub(r"[\d,.()\-]+", " ", line) or LINE_ALIASES[key][0]
        texts = [pages[page]] if page in pages else list(pages.values())
        grounded = bool(line) and any(number_on_line(t, printed, label) for t in texts)
        out.append(Reading(key, round(printed * factor, 6), printed, page, line[:200], grounded))
    return out


def vote(values: list[LineValue], readings: list[Reading], scale: float = 1.0) -> SecondReading:
    """Compare the table reader with the grounded answers of the model, key by key. ``scale`` turns the model's
    crore of the printed currency into the values' money unit (money for any country: LKR million, FX rate)."""
    result = SecondReading()
    if scale != 1.0:
        readings = [Reading(r.key, round(r.value * scale, 6), r.printed, r.page, r.line, r.grounded) for r in readings]
    ours = {v.key: v for v in values if not v.derived}
    for r in readings:
        if not r.grounded:
            result.dropped += 1
            continue
        other = abs(r.value) if r.key in _EXPENSES else r.value  # expenses are stored positive
        v = ours.get(r.key)
        if v is None:
            result.suggestions.append({"key": r.key, "label": r.line, "value": other, "page": r.page, "cell": None,
                                       "source": "second reader"})
        elif abs(v.value - other) <= max(0.01, TOLERANCE * abs(v.value)):
            result.confirmed.append(r.key)
        else:
            result.issues.append(f"SECOND_READER_DIFFERS: {r.key} read {v.value}, second reader {other}")
    return result


def disagreements(values: list[LineValue], theirs: dict[str, float]) -> list[str]:
    """One issue per covenant input the two readers read differently (kept for plain value maps)."""
    readings = [Reading(k, v, v, None, "", True) for k, v in theirs.items() if k in CHECKED]
    return vote(values, readings).issues


def check(text: str, values: list[LineValue], completion: Completion, entity: str = "STANDALONE",
          scale: float = 1.0) -> SecondReading:
    """The second reading; an empty one when the model fails (a check, not a step the flow depends on)."""
    try:
        return vote(values, ai_read(text, completion, entity), scale)
    except Exception as ex:  # noqa: BLE001 - a model outage must not stop statements from being read
        log.warning("Second reader unavailable: %s", type(ex).__name__)
        return SecondReading()
