"""Flow A Extract step, optional: a second reader for covenants (Azure OpenAI), grounded and voting.

The rules (agreement.py) and the safety net (coverage.py) run first. When ``DPS_AGREEMENT_AI_CHECK`` is on and a
model is configured, the model reads the covenant pages (the financial covenants section, and every page where
the rules or the safety net found something) and lists the maintenance covenants, each with a verbatim quote.

1. **Grounding** (verify.py): a covenant counts only if its quote is on the cited page (or the one next to it)
   and its threshold is written in the quote. Anything else is dropped.
2. **Voting**, covenant by covenant:
   - rules and model read the same covenant and number: **confirmed** (confidence 0.97, method ``REGEX+AI``);
   - same covenant, another number: the rules' covenant waits for a person (``AI_READ_DIFFERENT``);
   - another covenant of a family the rules found (senior secured leverage beside total leverage), or a covenant
     only the model found: proposed for review (``AI_ONLY``), and a safety-net sentence it explains is dropped.

The rules' covenants stay the source; the model only confirms, questions or adds proposals for a person.
Benchmark (outputs/benchmarks/real-documents/, section 13): see the report for what this adds.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from dps.ai_guard import DATA_NOT_INSTRUCTIONS
from dps.extraction.agreement import AMOUNT_METRICS, MAX_RATIO, Term, _name_key
from dps.extraction.verify import normalize, quote_on_page, threshold_in
from dps.language import detect as detect_language
from dps.language import plain_digits
from dps.parsing import Page

log = logging.getLogger(__name__)
Completion = Callable[[list[dict]], str]
MAX_CHARS = 60000
MAX_PAGES = 8

# The model's covenant families -> DPS metrics (a name decides between alternatives).
FAMILY_METRIC = {"LEVERAGE": "DEBT_TO_EBITDA", "DSCR": "DSCR", "ICR": "ICR", "FCCR": "FIXED_CHARGE_COVERAGE_RATIO",
                 "DEBT_TO_NW": "DEBT_TO_NET_WORTH", "CURRENT_RATIO": "CURRENT_RATIO", "MIN_NW": "MIN_NET_WORTH",
                 "LIQUIDITY": "MINIMUM_LIQUIDITY", "MIN_EBITDA": "MIN_EBITDA", "LTV": "LOAN_TO_VALUE",
                 "DEBT_TO_EQUITY": "DEBT_TO_EQUITY"}
# Metrics that are one covenant family for voting (net and total leverage both answer "leverage").
GROUP = {"NET_LEVERAGE_RATIO": "DEBT_TO_EBITDA", "MIN_TANGIBLE_NET_WORTH": "MIN_NET_WORTH",
         "DEBT_TO_EQUITY": "DEBT_TO_NET_WORTH", "EBITDA_TO_EMI": "DSCR"}

PROMPT = """You read part of a loan or credit agreement for a bank's covenant monitoring.
List the FINANCIAL MAINTENANCE COVENANTS only: ratios or amounts the borrower must keep meeting while the loan
is outstanding. Do NOT list pricing grids, incurrence tests for new debt or payments ("on a pro forma basis"),
excess cash flow percentages, baskets, conditions for releasing funds or extending the loan, or insurance
amounts. When a covenant steps down over time, give the first level.
Return JSON only: {{"covenants": [{{"name": the covenant's name as written, "family": one of LEVERAGE, DSCR, ICR,
FCCR, DEBT_TO_NW, DEBT_TO_EQUITY, CURRENT_RATIO, MIN_NW, LIQUIDITY, MIN_EBITDA, LTV, OTHER, "dir": "min" or "max",
"threshold": the number as written in digits (3.5 for "3.50 to 1.00", 75 for "75%", 500 for "INR 500 crores"),
"unit": "crore", "lakh", "million" or null, "page": page number,
"quote": the sentence that sets it, copied word for word from the page}}]}}
Pages are marked "=== Page N ===".
The agreement may be in any language: give "name" in English, and copy "quote" exactly as printed, in the
agreement's own language and script.
{guard}

{text}"""

_HEAD = re.compile(r"financial\s+(maintenance\s+)?covenants?|financial\s+condition\s+covenants?|financial\s+ratios|"
                   r"financial\s+undertakings", re.I)
_KEEP = re.compile(r"(shall|will)\s+not\s+permit[^.]{0,160}(ratio|net\s+worth|liquidity)|"
                   r"(shall|will)\s+(at\s+all\s+times\s+)?maintain[^.]{0,120}(ratio|net\s+worth|liquidity|coverage)|"
                   r"(not|never)\s+(be\s+)?(less|greater|more)\s+than\s+[\d.]+\s*(to|:)\s*1", re.I)
_NOISE = re.compile(r"applicable\s+rate|pricing\s+level|pro\s+forma\s+basis|excess\s+cash\s+flow|incremental|"
                    r"asset\s+sale\s+percentage|ecf\s+percentage", re.I)


def covenant_pages(pages: list[Page], terms: list[Term]) -> list[Page]:
    """The pages to send: the covenant section (scored) and every page the rules or the safety net used."""
    scored = sorted(((5 if _HEAD.search(p.text) else 0) + 3 * len(_KEEP.findall(p.text))
                     - 2 * len(_NOISE.findall(p.text)), p.no) for p in pages)
    chosen = [no for score, no in reversed(scored[-3:]) if score > 0]
    if not chosen and detect_language(" ".join(p.text or "" for p in pages)) != "en":
        # Phase 3b-2: an agreement in another language has none of the English headings: the pages with the most
        # ratios and amounts ("1,25", "3.50", "75 %") are where its covenants are.
        numeric = sorted(((len(_NUMERIC.findall(plain_digits(p.text or ""))), p.no) for p in pages), reverse=True)
        chosen = [no for count, no in numeric[:MAX_PAGES] if count > 0]
    for t in terms:
        if t.category in ("FINANCIAL", "CANDIDATE") and t.page is not None and t.page not in chosen:
            chosen.append(t.page)
    by_no = {p.no: p for p in pages}
    return [by_no[n] for n in sorted(chosen[:MAX_PAGES]) if n in by_no]


def _metric(family: str, name: str) -> str | None:
    if family == "LEVERAGE" and re.search(r"\bnet\b", name or "", re.I):
        return "NET_LEVERAGE_RATIO"
    if family == "MIN_NW" and re.search(r"tangible", name or "", re.I):
        return "MIN_TANGIBLE_NET_WORTH"
    return FAMILY_METRIC.get(family)


def ai_covenants(pages: list[Page], terms: list[Term], completion: Completion) -> tuple[list[Term], int]:
    """The model's covenants that pass grounding, as Terms (method AI); and how many were dropped."""
    sent = covenant_pages(pages, terms)
    if not sent:
        return [], 0
    text = "\n\n".join(f"=== Page {p.no} ===\n{p.text}" for p in sent)[:MAX_CHARS]
    answer = completion([{"role": "user", "content": PROMPT.format(text=text, guard=DATA_NOT_INSTRUCTIONS)}])
    data = json.loads(answer[answer.find("{"): answer.rfind("}") + 1])
    texts = {p.no: p.text for p in pages}
    out, dropped = [], 0
    for c in data.get("covenants") or []:
        if not isinstance(c, dict) or not isinstance(c.get("threshold"), (int, float)):
            dropped += 1
            continue
        threshold = float(c["threshold"])
        quote = str(c.get("quote") or "")
        page = quote_on_page(texts, c.get("page") if isinstance(c.get("page"), int) else None, quote)
        if page is None or not threshold_in(quote, threshold):
            dropped += 1  # not on the page, or the number is not in it: never used
            continue
        if _FORM.search(quote):
            dropped += 1  # a compliance-certificate form ("Leverage Ratio is: [____]"), not the covenant
            continue
        name = str(c.get("name") or "").strip()[:120]
        metric = _metric(str(c.get("family") or "").upper(), name)
        op = ">=" if str(c.get("dir")).lower() == "min" else "<="
        unit = c.get("unit") if c.get("unit") in ("crore", "lakh", "million") else None
        if metric == "LOAN_TO_VALUE":
            unit = "%"
        elif metric in _COVERAGE and threshold > MAX_RATIO and "%" in quote:
            threshold = round(threshold / 100, 6)  # "Cash Flow Coverage of not less than 125%" is 1.25 times
        elif metric not in AMOUNT_METRICS and threshold > MAX_RATIO:
            metric = None  # "Total Liabilities to Tangible Assets of no greater than 70%": not this ratio
        elif metric in AMOUNT_METRICS and unit is None and threshold < 1000:
            metric = None  # "85% of Tangible Net Worth": a formula, not an amount
        if any(a.metric == metric and a.operator == op and a.threshold == threshold for a in out):
            continue  # the same covenant quoted from another page
        if metric is None:
            out.append(Term("POSSIBLE_COVENANT", "CANDIDATE", None, None, None, None, page, normalize_quote(quote),
                            None, 0.6, method="AI", issues=["AI_ONLY: found by the AI reader, not by the rules"],
                            name=name or "Covenant found by the AI reader"))
            continue
        out.append(Term(metric, "FINANCIAL", op, threshold, unit, None, page, normalize_quote(quote), None, 0.6,
                        method="AI", issues=["AI_ONLY: found by the AI reader, not by the rules"],
                        name=name or None))
    return out, dropped


_NUMERIC = re.compile(r"\d+[.,]\d+|\d+\s*%")
_FORM = re.compile(r"_{4,}|\[\s*_+\s*\]|\bYes:\s|\bNot in compliance\b", re.I)
_COVERAGE = {"DSCR", "ICR", "FIXED_CHARGE_COVERAGE_RATIO", "CURRENT_RATIO"}


def normalize_quote(quote: str) -> str:
    return re.sub(r"\s+", " ", quote).strip()[:480]


def _family(metric: str) -> str:
    return GROUP.get(metric, metric)


def _same_number(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= max(0.011, 0.01 * abs(b))


def vote(terms: list[Term], ai: list[Term]) -> list[Term]:
    """The rules' terms with the model's verdict on each, plus what only the model found."""
    out = list(terms)
    for a in ai:
        if a.category == "CANDIDATE":
            out.append(a)
            continue
        family = [t for t in out if t.category == "FINANCIAL" and t.method != "AI" and _family(t.metric) == _family(
            a.metric)]
        same = next((t for t in family if t.operator == a.operator and _same_number(t.threshold, a.threshold)), None)
        if same is not None:
            same.confidence = max(same.confidence, 0.97)
            same.method = "REGEX+AI"
            continue
        named = next((t for t in family if t.name and a.name and _name_key(t.name) == _name_key(a.name)), None)
        if named is None and len(family) == 1 and not (a.name and family[0].name):
            named = family[0]  # one covenant of the family and no names to tell them apart
        if named is not None:
            named.issues.append(f"AI_READ_DIFFERENT: {a.operator} {a.threshold:g} on page {a.page}")
            continue
        if a.metric in AMOUNT_METRICS and a.unit is None and "$" in a.quote:
            a.unit = "usd"
        out.append(a)
    # A safety-net sentence the model has turned into a proposal no longer needs its own row.
    added = [a for a in ai if a in out]
    return [t for t in out if not (t.category == "CANDIDATE" and t.method != "AI" and any(
        a.page == t.page and normalize(a.quote)[:60] in normalize(t.quote) for a in added))]


def read(pages: list[Page], terms: list[Term], completion: Completion) -> list[Term]:
    """Rules' terms after the model's vote; the rules' terms unchanged when the model fails."""
    try:
        ai, dropped = ai_covenants(pages, terms, completion)
    except Exception as ex:  # noqa: BLE001 - a model outage must not stop agreements from being read
        log.warning("AI covenant reader unavailable: %s", type(ex).__name__)
        return terms
    if dropped:
        log.info("AI covenant reader: %d answers not found on their page, dropped", dropped)
    return flag_unconfirmed(vote(terms, ai))


def flag_unconfirmed(voted: list[Term]) -> list[Term]:
    """Benchmark: a rules covenant the model did not confirm was right only 26% of the time (6 of 23), against
    89% for one both read the same, so it says so and waits for a person."""
    for t in voted:
        if t.category == "FINANCIAL" and t.method == "REGEX" and not any(
                i.startswith("AI_READ_DIFFERENT") for i in t.issues):
            t.issues.append("AI_DID_NOT_CONFIRM")
    return voted
