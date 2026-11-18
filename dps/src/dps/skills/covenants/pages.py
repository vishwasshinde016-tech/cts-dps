"""Which pages of an agreement the Document Agent reads for covenants (pack ``page_selection.rules: covenant_section``).

Before 18 Nov 2026 the agent saw only the pages where the rules or the safety net had found something, so a covenant
the rules missed on any other page could never be found. Now every page is scored without a model call, and pages
are taken in this order until the page and character budget is used:

1. pages the rules or the safety net used;
2. covenant section pages: a heading such as "Financial Covenants", "Negative Covenants", "Information
   Undertakings", "Affirmative Covenants" or the compliance certificate schedule, and the page after it (sections
   run on);
3. pages with duty sentences (an obligation word with a comparison or a financial word), most first;
4. the definitions pages (where "EBITDA", "Total Debt" and the ratios are defined).

Pages left out stay reachable: the agent's tools search and open any page of the document.
"""

from __future__ import annotations

import re

from dps.extraction.coverage import _COMPARISON, _FINANCIAL, _OBLIGATION

HEADING = re.compile(
    r"(?im)^[ \t]*(?:article|section|clause|part|schedule)?[ \t]*[\dIVXLC]*[.)]?[\d.]*[ \t]*[-–:]?[ \t]*"
    r"(?:financial\s+(?:maintenance\s+)?covenants?|financial\s+condition\s+covenants?|financial\s+ratios|"
    r"financial\s+undertakings?|(?:affirmative|negative|positive|general|information|reporting)\s+"
    r"(?:covenants?|undertakings?)|covenants?|undertakings?|form\s+of\s+compliance\s+certificate|"
    r"compliance\s+certificate)\b[^\n]{0,60}$")
DEFINITIONS = re.compile(r"(?im)^[ \t]*(?:article|section|clause)?[ \t]*[\dIVX.]*[ \t]*(?:definitions|"
                         r"definitions\s+and\s+interpretation|interpretation)\b")
DEFINED_TERM = re.compile(r"[\"“'][^\"”'\n]{2,60}[\"”']\s+(?:means|shall\s+mean|has\s+the\s+meaning)", re.I)
METRIC_WORD = re.compile(r"ebitda|debt|net\s+worth|coverage|leverage|interest|liquidity|equity|cash\s+flow", re.I)
SENTENCE = re.compile(r"[^.;\n]*(?:[.;]|\n|$)")
RESERVED_CHARS = 10_000  # room in the prompt for the skill text and schema


def duty_sentences(text: str) -> int:
    """Sentences that set a duty a covenant could come from: an obligation word with a comparison or a financial
    word ("shall maintain ... not less than", "shall not permit ... Debt")."""
    count = 0
    for m in SENTENCE.finditer(text or ""):
        s = m.group(0)
        if len(s) > 12 and _OBLIGATION.search(s) and (_COMPARISON.search(s) or _FINANCIAL.search(s)):
            count += 1
    return count


def _definition_score(text: str) -> int:
    terms = DEFINED_TERM.findall(text or "")
    return (5 if DEFINITIONS.search(text or "") else 0) + sum(1 for t in terms if METRIC_WORD.search(t))


def covenant_pages(pages: list[dict], candidates: list[dict], max_pages: int = 25,
                   max_chars: int = 60_000) -> tuple[list[dict], dict]:
    """The pages to send, in page order, and a note of why each was chosen and which duty pages did not fit."""
    if not pages or max_pages <= 0:
        return [], {"sent": [], "notSent": []}
    by_no = {p["no"]: p for p in pages}
    order: list[tuple[int, str]] = []

    def add(no: int | None, why: str) -> None:
        if no in by_no and all(no != n for n, _ in order):
            order.append((no, why))

    for f in candidates:
        add((f.get("source") or {}).get("page"), "RULES")
    numbers = [p["no"] for p in pages]
    for i, p in enumerate(pages):
        if HEADING.search(p.get("text") or ""):
            add(p["no"], "SECTION")
            if i + 1 < len(numbers):
                add(numbers[i + 1], "SECTION")
    duties = sorted(((duty_sentences(p.get("text") or ""), p["no"]) for p in pages), key=lambda d: (-d[0], d[1]))
    for count, no in duties:
        if count > 0:
            add(no, "DUTY")
    defs = sorted(((_definition_score(p.get("text") or ""), p["no"]) for p in pages), key=lambda d: (-d[0], d[1]))
    for score, no in defs[:2]:
        if score >= 3:
            add(no, "DEFINITIONS")
    if not order:  # no signal at all (a short or unusual document): the first pages
        order = [(no, "FIRST_PAGES") for no in numbers[:max_pages]]

    budget = max(max_chars - RESERVED_CHARS, 0)
    keep: dict[int, str] = {}
    used = 0
    for no, why in order:
        size = len(by_no[no].get("text") or "") + 40
        if len(keep) >= max_pages or (keep and used + size > budget):
            continue
        keep[no] = why
        used += size
    not_sent = [no for no, why in order if no not in keep and why in ("RULES", "SECTION", "DUTY")]
    chosen = [p for p in pages if p["no"] in keep]
    return chosen, {"sent": [{"page": no, "why": keep[no]} for no in sorted(keep)], "notSent": sorted(not_sent)}
