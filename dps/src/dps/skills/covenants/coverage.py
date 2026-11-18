"""Coverage check (18 Nov 2026): every duty sentence on the covenant pages is explained by a field, or listed.

A duty sentence is an obligation word with a comparison, a financial word or a non-financial covenant subject
(liens, disposals, change of business, reporting, insurance ...), as in ``pages.duty_sentences``. It is
explained when a covenant or schedule-term field on the same page or a neighbour quotes it (one text contains the
other, or most of their words are shared). Loan mechanics (drawdown, prepayment, repayment, fees) are left to the
schedule skill and do not count. What is left is shown to the reviewer, so a covenant nobody extracted cannot pass
silently.
"""

from __future__ import annotations

import re

from dps.skills.covenants.pages import is_duty, sentences

MECHANICS = re.compile(r"draw\s?down|utili[sz]ation|prepay|repay|instal+ment|"
                       r"interest\s+(?:shall\s+be\s+)?pa(?:id|yable)|"
                       r"\bfees?\b|commission|tenor|availability\s+period", re.I)
WORD = re.compile(r"[a-z0-9]+")
DEFINES = re.compile(r"\bmeans\b|shall\s+mean|has\s+the\s+meaning", re.I)
MAX_LISTED = 10


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall((text or "").lower()) if len(w) > 2}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1\2", text or "")).strip().lower()


def duties(text: str) -> list[str]:
    return [s for s in sentences(text) if is_duty(s) and not MECHANICS.search(s) and not DEFINES.search(s)]


def _explains(quote: str, sentence: str) -> bool:
    q, s = _flat(quote), _flat(sentence)
    if len(q) >= 12 and (q in s or s in q):
        return True
    qw, sw = _words(q), _words(s)
    return bool(sw) and len(qw & sw) / len(sw) >= 0.6


def check(pages: list[dict], fields: list[dict]) -> dict:
    """{dutySentences, explained, percent, unexplained: [{page, text}]} over ``pages``."""
    quotes: dict[int, list[str]] = {}
    for f in fields:
        src = f.get("source") or {}
        if src.get("page") is not None and src.get("quote"):
            quotes.setdefault(int(src["page"]), []).append(str(src["quote"]))
    total, explained, missing = 0, 0, []
    for p in pages:
        no = int(p["no"])
        near = quotes.get(no, []) + quotes.get(no - 1, []) + quotes.get(no + 1, [])
        for s in duties(p.get("text") or ""):
            total += 1
            if any(_explains(q, s) for q in near):
                explained += 1
            else:
                missing.append({"page": no, "text": s[:200]})
    percent = round(100.0 * explained / total, 1) if total else 100.0
    return {"dutySentences": total, "explained": explained, "percent": percent,
            "unexplained": missing[:MAX_LISTED], "unexplainedCount": len(missing)}
