"""Flow A safety net: every sentence that reads like a financial covenant is either explained or shown to a person.

The covenant rules (agreement.py) know a list of covenants and of ways to write them. An agreement written
tomorrow may use a covenant or a wording they do not know ("Debt Yield of not less than 9%"). This pass does
not try to understand the sentence. It only recognises the shape of a covenant, an obligation, a comparison,
a financial word and a number, and checks that each such sentence is accounted for:

- part of a proposed covenant (its quote overlaps the sentence), or
- set aside for a known reason: a pricing grid, an incurrence test, a cash sweep, a definition;
- otherwise proposed as a POSSIBLE_COVENANT for a person: "not understood by DPS, please look".

So a covenant DPS cannot read is never dropped without a trace.
"""

from __future__ import annotations

import re

from dps.extraction.agreement import (
    _DEFINED_COMPARISON,
    _NOT_A_COVENANT,
    Term,
    _defined_numbers,
    _Doc,
    normalize_ws,
)
from dps.parse.engine import Page

# "permit"/"maintain" too: a list form ("the Borrower will not: ... 6.01 Permit the Leverage Ratio to exceed")
# leaves the "will not" in an earlier sentence.
# Held-out set: an item of a "Permit: ... (f) the Borrower to have a ratio ... of less than 1.15 to 1.0" list
# holds the prohibited state; its "Permit" is in the lead-in.
_STATE = re.compile(r"\bto\s+(?:be|have)\b[^.;]{0,400}?\b(?:less|greater|more)\s+than\b", re.I)
_OBLIGATION = re.compile(r"\b(shall|will|must|agrees?\s+to|undertakes?|covenants?\s+(?:that|to)|is\s+required\s+to|"
                         r"permit|maintain)\b|" + _STATE.pattern, re.I)
_COMPARISON = re.compile(r"not\s+(?:be\s+)?(?:less|lower|more|greater|higher)\s+than|at\s+least|\bminimum\b|"
                         r"\bto\s+be\s+(?:less|greater|more)\s+than\b|\bof\s+(?:less|greater|more)\s+than\b|"
                         r"\bmaximum\b|\bexceed|\bbelow\b|\babove\b|or\s+(?:less|more|greater)\b|no\s+(?:less|more|greater)\s+than|"
                         r"in\s+excess\s+of|fall\s+below|\bmaintain|"
                         # Bug hunt (8 November 2026): "is at all times greater than or equal to 2.5x", "shall not
                         # at any time be lower than", "to go beyond", "at or above".
                         r"\b(?:less|lower|more|greater|higher)\s+than\b|\bequal\s+to\b|\bbeyond\b|"
                         r"\bat\s+or\s+(?:above|below)\b|\bat\s+most\b", re.I)
_FINANCIAL = re.compile(r"\bratio\b|coverage|\bcover\b|leverage|net\s+worth|tangible|\bebitda\b|liquidity|"
                        r"\bcash\b|\bdebt\b|\bequity\b|\bcapital\b|\bvalue\b|\byield\b|gearing|borrowings|"
                        r"indebtedness|\bassets\b|\bincome\b|\bearnings\b|\bprofit\b", re.I)
_WORD = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)"
_NUMBER = re.compile(r"\b" + _WORD + r"(?:\s+point(?:\s+" + _WORD + r"){1,3}|"
                     r"\s+and\s+(?:one[- ]|a[- ])?(?:half|quarter)|\s+and\s+three[- ]quarters?)?"
                     r"[\s-]+to[\s-]+one\b|"  # held-out set: "not greater than one-to-one"
                     r"\d+(?:\.\d+)?\s*(?:%|per\s*cent|percent|x\b|times\b|:\s*1(?:\.0+)?\b|to\s+1(?:\.0+)?\b)|"
                     r"(?:\$|INR|Rs\.?|₹|LKR|USD)\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:crores?|lakhs?|million|billion)\b|"
                     # Wording stress test: a plain ratio ("a minimum DSCR of 1.25"), not a section number ("7.12 ...",
                     # "Section 6.01") nor a date.
                     r"(?<![\w.§/-])(?<!section\s)(?<!clause\s)(?<!article\s)\d{1,2}\.\d{1,2}\b(?![\d/-])(?!\.\d)(?!\s+(?-i:[A-Z][a-z]))",
                     re.I)
# A row of a covenant table has no "shall": "Debt Service Coverage Ratio  Minimum 1.25x".
_TABLE_ROW = re.compile(r"(?im)^[^\n]{0,80}?\b(?:ratio|coverage|cover|leverage|net\s+worth|liquidity|liquid\s+assets|"
                        r"yield|gearing|ebitda|ltv|tol/tnw|unrestricted\s+cash|dscr|icr|fccr)\b[^\n]{0,40}?\b(?:minimum|"
                        r"min\.?|maximum|max\.?|not\s+less\s+than|not\s+more\s+than|at\s+least|at\s+most|>=|<=|≥|≤)"
                        r"[^\n]{0,20}?(?:\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\b)"
                        # Bug hunt 3: the label form, "Minimum DSCR: 1.15x", "Max. Debt/EBITDA: 5.0x".
                        r"|(?:^[^\w\n]*|[:;(]\s*)(?:minimum|min\.?|maximum|max\.?)\s[^\n:]{0,60}?\b(?:ratio|coverage|cover|leverage|"
                        r"net\s+worth|liquidity|yield|gearing|ebitda|ltv|tol/tnw|dscr|icr|fccr)\b[^\n:]{0,20}:\s*"
                        r"(?:\d|(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\b)")
_DEFINITION = re.compile(r"^[\W\d]*[\"“][^\"”]{2,80}[\"”]\s+(?:means|shall\s+mean|has\s+the\s+meaning)", re.I)
_NAME = re.compile(r"((?:[A-Z][\w\-]*\s+){0,4}(?:Ratio|Coverage|Cover|Leverage|Net\s+Worth|EBITDA|Liquidity|Yield|"
                   r"Gearing|Value|Capital|Margin))\b")
# Permissions with a cap, not a test the borrower must pass: "Investments ... not to exceed the greater of $73.0
# million and 45.0% of LTM EBITDA", "Liens securing Indebtedness ...", "as if such Indebtedness had been incurred".
_BASKET = re.compile(r"\b(?:investments?|liens?|dispositions?|restricted\s+payments?|judgments?)\b[^.]{0,200}"
                     r"(?:aggregate|greater\s+of|fair\s+market\s+value)|had\s+been\s+incurred|such\s+incurrence|"
                     r"release\s+price|partial\s+release|qualify\s+for|in\s+tranches", re.I)
# More than this is noise for a reviewer; the rest are counted in the summary issue.
MAX_SHOWN = 12


def _sentences(text: str) -> list[str]:
    # Sri Lanka run (Dialog): "(ii) ... 6.0 times; and (iii) a Debt Service Coverage Ratio ..." was one sentence, so
    # the interest cover found in (ii) hid the DSCR in (iii). Each list item is a sentence of its own.
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(\"“])|\n\s*\n|\s+(?=\((?:[ivx]{1,4}|[a-h])\)\s)", text)
    # A short part counts when it names a covenant and holds a number ("Minimum DSCR: 1.15x;", bug hunt 3).
    return [normalize_ws(p) for p in parts if len(p) > 30 or _METRIC.search(p) and re.search(r"\d", p)]


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def _covered(sentence: str, proposals: list[tuple[str, float | None]]) -> bool:
    """A proposal explains the sentence when it quotes it and its own number is in it: a proposal with another
    number (the covenant read at a different level, or a different covenant nearby) leaves it unexplained."""
    words = set(re.findall(r"[a-z0-9.]+", sentence.lower()))
    numbers = _numbers(sentence)
    for q, threshold in proposals:
        if threshold is not None and not any(abs(n - threshold) < 1e-9 for n in numbers):
            continue
        qn = normalize_ws(q)
        if qn and (qn in sentence or sentence in qn):
            return True
        qwords = set(re.findall(r"[a-z0-9.]+", qn.lower()))
        if qwords and len(words & qwords) >= 0.6 * min(len(words), len(qwords)):
            return True
    return False


_METRIC = re.compile(r"\bratio\b|coverage|\bcover\b|leverage|net\s+worth|liquidity|liquid\s+assets|\byield\b|gearing|"
                     r"\bebitda\b|loan[- ]to[- ]value|\bltv\b|tol/tnw|\btnw\b|unrestricted\s+cash|\bdscr\b|\bicr\b|"
                     r"\bfccr\b", re.I)


def looks_like_covenant(sentence: str) -> bool:
    """An obligation, and a limit next to a covenant word: "... Debt Yield ... not less than 9%".

    The limit must sit close to both a comparison and a covenant word (within about 160 characters before it), so
    the long legal sentences about baskets, prepayments and eligible investments, which also hold "shall", a
    number and a financial word somewhere, are not shown.
    """
    if _TABLE_ROW.search(sentence) and not re.search(r"applicable\s+(?:margin|rate)|pricing|level\s+[ivx\d]",
                                                     sentence, re.I):
        return True  # a covenant table: name, "Minimum"/"Maximum" and the number on one line
    if not _OBLIGATION.search(sentence):
        return False
    for state in _STATE.finditer(sentence):
        # "the Borrower to have a ratio of EBITDA to Interest Expense with respect to (i) ... (iii) ..., of less
        # than 1.15 to 1.0": the covenant word is far from the number, but inside the prohibited state.
        if _METRIC.search(state.group(0)) and _NUMBER.match(sentence[state.end():].lstrip()):
            return True
    for m in _NUMBER.finditer(sentence):
        near = sentence[max(0, m.start() - 160):m.end() + 30]
        if _COMPARISON.search(near) and _METRIC.search(near):
            return True
    return False


def possible_covenants(pages: list[Page], terms: list[Term]) -> tuple[list[Term], dict[str, int]]:
    """Covenant-shaped sentences no proposal explains, and how many were set aside for each known reason."""
    quotes_by_page: dict[int | None, list[tuple[str, float | None]]] = {}
    for t in terms:
        if t.category == "FINANCIAL" and t.quote:
            quotes_by_page.setdefault(t.page, []).append((t.quote, t.threshold))
    all_quotes = [q for qs in quotes_by_page.values() for q in qs]
    set_aside = {"pricing, incurrence or other condition": 0, "definition": 0}
    seen: set[str] = set()
    found: list[Term] = []
    # A covenant whose limit is a defined word ("is less than the Debt Yield Minimum", where "Debt Yield Minimum"
    # means 9.0%) holds no number; it counts when it compares against such a word.
    defined = [n for n in _defined_numbers(_Doc(pages))] if pages else []
    against_defined = re.compile(r"(?:" + "|".join(p for p, _ in _DEFINED_COMPARISON) + r")\s+(?:the\s+)?(?:"
                                 + "|".join(re.escape(n) for n in defined) + r")\b", re.I) if defined else None
    for page in pages:
        for sentence in _sentences(page.text):
            if not (looks_like_covenant(sentence) or against_defined and against_defined.search(sentence)):
                continue
            key = re.sub(r"\W+", " ", sentence.lower())[:200]
            if key in seen:
                continue
            seen.add(key)
            same_page = quotes_by_page.get(page.no, [])
            numbers = _numbers(sentence)
            if any(t is not None and any(abs(n - t) < 1e-9 for n in numbers) for _, t in same_page):
                continue  # a covenant on this page with this number (quoted from its "Required threshold" line)
            if _covered(sentence, same_page or all_quotes):
                continue
            if _DEFINITION.search(sentence):
                set_aside["definition"] += 1
                continue
            if _NOT_A_COVENANT.search(sentence) or _BASKET.search(sentence):
                set_aside["pricing, incurrence or other condition"] += 1
                continue
            name = _NAME.search(sentence)
            found.append(Term("POSSIBLE_COVENANT", "CANDIDATE", None, None, None, None, page.no, sentence[:480],
                              None, 0.5, issues=["POSSIBLE_COVENANT_NOT_UNDERSTOOD"],
                              name=normalize_ws(name.group(1)) if name else "Possible covenant"))
    if len(found) > MAX_SHOWN:
        extra = len(found) - MAX_SHOWN
        found = found[:MAX_SHOWN]
        found[-1].issues.append(f"MORE_POSSIBLE_COVENANTS: {extra} more covenant-like sentences not shown")
    return found, set_aside
