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

from dps.extraction.safety_net import _COMPARISON, _FINANCIAL, _OBLIGATION

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
# A sentence ends at ". " or "; " or a blank line; single line breaks inside a paragraph are joined (PDF text).
SENTENCE_END = re.compile(r"(?<=[.;])\s+|\n[ \t]*\n")
# Not duties: lead-ins ("the Borrower shall comply with the following covenants:") and what a breach leads to.
NOT_A_DUTY = re.compile(r":\s*$|\bevents?\s+of\s+default\b|\bshall\s+constitute\b|"
                        # the parties clause ("which expression shall, unless repugnant, include its successors"),
                        # interpretation ("any reference to the Lender shall include its successors") and the
                        # grant of the loan ("the Lender agrees to grant"): the agreement's mechanics, not duties.
                        r"\bexpression\s+shall\s*,?\s*(?:unless|include|mean|be\s+deemed)|"
                        r"\breferences?\s+(?:in\s+this\s+agreement\s+)?to\b[^.;]{0,120}?\bshall\s+(?:include|be\s+construed|mean)|"
                        r"\blender\s+(?:hereby\s+)?agrees?\s+to\s+(?:grant|lend|make\s+available|advance)", re.I)
NON_FINANCIAL_DUTY = re.compile(
    r"\b(?:lien|charge|encumbrance|pledge|mortgage|security\s+interest|dispos|sell|transfer|lease|merge|amalgamat|"
    r"consolidat|demerge|reorgani[sz]|dividend|distribution|guarantee|invest|lend|loan|borrow|indebtedness|"
    r"change\s+(?:in|of)|nature\s+of\s+(?:its\s+)?business|control|shareholding|constitution|articles|deliver|"
    r"furnish|provide|submit|notify|inform|insur|comply|maintain|keep|books|records|inspect|tax|licen[cs]|consent)",
    re.I)
BORROWER_DUTY = re.compile(r"\b(?:borrower|obligor|guarantor|company|issuer|promoter)s?\b[^.;]{0,80}?"
                           r"\b(?:shall|will|must|undertakes?|agrees?|covenants?)\b", re.I)


# Conditions precedent are one-time conditions before a drawdown, not covenants: "the Borrower shall have delivered",
# "shall execute and deliver to the Lender", "conditions precedent".
CONDITION_PRECEDENT = re.compile(r"\bshall\s+have\s+(?:been\s+)?(?:\w+(?:ed|en|ne)|paid|made|set)\b|"
                                 r"\bconditions?\s+precedent\b|"
                                 r"\bexecute\s+and\s+deliver\b|"
                                 r"\bprior\s+to\s+(?:the\s+)?(?:first\s+)?(?:drawdown|disbursement)", re.I)


def is_duty(sentence: str) -> bool:
    """A financial duty (obligation with a comparison or a financial word), or a non-financial one that binds
    the borrower ("the Borrower shall not create any lien"; not "the Lender shall disburse the Loan")."""
    if len(sentence) <= 12 or not _OBLIGATION.search(sentence) or CONDITION_PRECEDENT.search(sentence) \
            or NOT_A_DUTY.search(sentence):
        return False
    if _COMPARISON.search(sentence) or _FINANCIAL.search(sentence):
        return True
    return bool(NON_FINANCIAL_DUTY.search(sentence) and BORROWER_DUTY.search(sentence))
RESERVED_CHARS = 10_000  # room in the prompt for the skill text and schema


def sentences(text: str) -> list[str]:
    """The sentences of a page, each on one line."""
    return [" ".join(s.split()) for s in SENTENCE_END.split(text or "") if s and s.strip()]


def duty_sentences(text: str) -> int:
    """Sentences that set a duty a covenant could come from: an obligation word with a comparison, a financial
    word or a non-financial covenant subject ("shall maintain ... not less than", "shall not permit ... Debt",
    "shall not create any lien", "shall deliver its audited accounts")."""
    return sum(1 for s in sentences(text) if is_duty(s))


def _definition_score(text: str) -> int:
    terms = DEFINED_TERM.findall(text or "")
    return (5 if DEFINITIONS.search(text or "") else 0) + sum(1 for t in terms if METRIC_WORD.search(t))


def covenant_pages(pages: list[dict], candidates: list[dict], max_pages: int = 25,
                   max_chars: int = 60_000) -> tuple[list[dict], dict]:
    """The pages to send in one read, in page order, and a note of why each was chosen and which duty pages did not
    fit."""
    batches, note = covenant_batches(pages, candidates, max_pages, max_chars, max_batches=1)
    return (batches[0] if batches else []), note


def covenant_batches(pages: list[dict], candidates: list[dict], max_pages: int = 25, max_chars: int = 60_000,
                     max_batches: int = 1) -> tuple[list[list[dict]], dict]:
    """The pages to send as up to ``max_batches`` reads (long agreements: one prompt cannot hold every covenant page).
    Pages are taken in priority order; each batch keeps to ``max_pages`` and the prompt budget and is in page order.
    The note says why each page was chosen, in which batch, and which duty pages fit in no batch."""
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
    batches: list[dict[int, str]] = [{}]
    used = [0]
    for no, why in order:
        size = len(by_no[no].get("text") or "") + 40
        placed = False
        for b, keep in enumerate(batches):  # the first batch it fits in
            if len(keep) < max_pages and (not keep or used[b] + size <= budget):
                keep[no] = why
                used[b] += size
                placed = True
                break
        if not placed and len(batches) < max(max_batches, 1):
            batches.append({no: why})
            used.append(size)
    kept = {no: (why, b + 1) for b, keep in enumerate(batches) for no, why in keep.items()}
    not_sent = [no for no, why in order if no not in kept and why in ("RULES", "SECTION", "DUTY")]
    out = [[p for p in pages if p["no"] in keep] for keep in batches if keep]
    return out, {"sent": [{"page": no, "why": kept[no][0], "batch": kept[no][1]} for no in sorted(kept)],
                 "notSent": sorted(not_sent)}
