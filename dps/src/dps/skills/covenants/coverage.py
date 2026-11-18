"""Coverage check (18 Nov 2026): every duty sentence on the covenant pages is explained by a field, or listed.

A duty sentence is an obligation word with a comparison, a financial word or a non-financial covenant subject
(liens, disposals, change of business, reporting, insurance ...), as in ``pages.duty_sentences``. Loan mechanics
(drawdown, prepayment, repayment, fees) are left to the schedule skill and do not count.

A duty sentence is explained:

1. **by quote**: a field on the same page or a neighbour quotes it (one text contains the other, or most of their
   words are shared); or
2. **by kind**: the sentence is about one covenant kind (insurance, reporting, liens, disposals, borrowing, control,
   books and records, compliance, or a financial metric such as DSCR) and a covenant of that same kind was extracted
   on the same page or a neighbour. One insurance clause of four "shall" sentences is one insurance covenant, so all
   four are explained by it. A financial sentence counts only for a covenant on the same metric.

A sentence of no known kind (an escrow account, for example) needs a quote. What is left is listed for the reviewer,
so a covenant nobody extracted cannot pass silently, without burying it under sentences that are already covered.
"""

from __future__ import annotations

import re

from dps.skills.covenants.names import FINANCIAL, OTHER, canonical
from dps.skills.covenants.pages import is_duty, sentences

MECHANICS = re.compile(r"draw\s?down|utili[sz]ation|prepay|repay|instal+ment|"
                       r"interest\s+(?:shall\s+be\s+)?pa(?:id|yable)|"
                       r"\bfees?\b|commission|tenor|availability\s+period", re.I)
WORD = re.compile(r"[a-z0-9]+")
DEFINES = re.compile(r"\bmeans\b|shall\s+mean|has\s+the\s+meaning", re.I)
MAX_LISTED = 10
COVENANT_GROUPS = {"FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "POSSIBLE_COVENANT", "INFO_COVENANT"}

# The kind of a duty sentence, checked in this order (the first match wins): insurance before reporting ("provide
# copies of the insurance policies" is about insurance), disposal before liens ("dispose of charged assets").
SENTENCE_KINDS: list[tuple[str, re.Pattern[str]]] = [(key, re.compile(words, re.I)) for key, words in [
    ("COMPLIANCE_CERTIFICATE", r"compliance\s+certificate|certificate\s+of\s+compliance"),
    ("INSURANCE_MAINTENANCE", r"\binsur|\bpremiums?\b|\bpolic(?:y|ies)\b|loss\s+payee|underwriter"),
    ("ASSET_DISPOSAL_RESTRICTION", r"\bdispos|\bsell\b|\bsale\s+of\b|transfer\s+(?:of\s+)?(?:any\s+(?:of\s+)?)?"
                                   r"(?:its\s+)?(?:assets|property|undertaking)|lease\s+out"),
    ("NEGATIVE_PLEDGE", r"\bliens?\b|encumbrance|\bpledge|\bmortgage|security\s+interest|\bcharge\s+over|"
                        r"ranks?\s+(?:in\s+)?priority|first[- ]ranking"),
    ("DEBT_INCURRANCE_RESTRICTION", r"\bborrow(?!er)|indebtedness|\bguarantee|\bincur\b|subordinat"),
    ("CHANGE_OF_CONTROL", r"\bcontrol\b|shareholding|ownership|\bpromoters?\b|\bmerge|amalgamat|demerg|reorgani|"
                          r"constitution|articles\s+of\s+association|nature\s+of\s+(?:its\s+)?business"),
    ("BOOKS_AND_RECORDS", r"\bbooks\b|\brecords\b|\binspect|\baudit\b|access\s+to\s+(?:its\s+)?(?:premises|"
                          r"books|records|facilities)"),
    ("REPORTING_COVENANT", r"\bdeliver|\bfurnish|\bsubmit|financial\s+statements?|\baccounts\b|\breports?\b|"
                           r"\bnotify|\bnotice\b|\binform\b|provide\s+(?:to\s+)?(?:the\s+lender\s+)?(?:with\s+)?"
                           r"(?:copies|a\s+copy|certified)"),
    ("REGULATORY_AND_TAX_COMPLIANCE", r"\bcomply|applicable\s+laws?|\btax(?:es)?\b|licen[cs]e|\bpermits?\b|"
                                      r"clearances?|environmental|anti-?corruption|money\s+laundering|sanctions"),
]]


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall((text or "").lower()) if len(w) > 2}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1\2", text or "")).strip().lower()


def duties(text: str) -> list[str]:
    return [s for s in sentences(text) if is_duty(s) and not MECHANICS.search(s) and not DEFINES.search(s)]


def sentence_kind(sentence: str) -> str | None:
    """The covenant kind a duty sentence is about: a financial metric named in it, else the first non-financial kind
    whose words it uses; None when it is about none of them."""
    metric = canonical(None, sentence)
    if metric in FINANCIAL:
        return metric
    return next((key for key, words in SENTENCE_KINDS if words.search(sentence)), None)


def field_kind(f: dict) -> str | None:
    """A covenant field's kind: its library metric key (None for OTHER and for fields that are not covenants)."""
    if f.get("group") not in COVENANT_GROUPS:
        return None
    value = f.get("value") or {}
    key = canonical(value.get("metric"), f.get("name"))
    return None if key == OTHER else key


def _explains(quote: str, sentence: str) -> bool:
    q, s = _flat(quote), _flat(sentence)
    if len(q) >= 12 and (q in s or s in q):
        return True
    qw, sw = _words(q), _words(s)
    return bool(sw) and len(qw & sw) / len(sw) >= 0.6


def check(pages: list[dict], fields: list[dict]) -> dict:
    """{dutySentences, explained, byQuote, byKind, percent, unexplained: [{page, text, kind}], unexplainedCount}."""
    quotes: dict[int, list[str]] = {}
    kinds: dict[int, set[str]] = {}
    for f in fields:
        src = f.get("source") or {}
        if src.get("page") is None:
            continue
        no = int(src["page"])
        if src.get("quote"):
            quotes.setdefault(no, []).append(str(src["quote"]))
        kind = field_kind(f)
        if kind:
            kinds.setdefault(no, set()).add(kind)
    total, by_quote, by_kind, missing = 0, 0, 0, []
    for p in pages:
        no = int(p["no"])
        near_quotes = quotes.get(no, []) + quotes.get(no - 1, []) + quotes.get(no + 1, [])
        near_kinds = kinds.get(no, set()) | kinds.get(no - 1, set()) | kinds.get(no + 1, set())
        for s in duties(p.get("text") or ""):
            total += 1
            if any(_explains(q, s) for q in near_quotes):
                by_quote += 1
                continue
            kind = sentence_kind(s)
            if kind and kind in near_kinds:
                by_kind += 1
                continue
            missing.append({"page": no, "text": s[:200], "kind": kind})
    explained = by_quote + by_kind
    percent = round(100.0 * explained / total, 1) if total else 100.0
    return {"dutySentences": total, "explained": explained, "byQuote": by_quote, "byKind": by_kind,
            "percent": percent, "unexplained": missing[:MAX_LISTED], "unexplainedCount": len(missing)}
