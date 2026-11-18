"""Phase 3 of "never miss silently": match a statement line DPS does not know by meaning, not exact words.

The catalog (statement.LINE_ALIASES) knows a few hundred exact wordings. A new statement may print "Net cash
inflow from trading activities", "Borrowings - non-current" or "Stock-in-trade". Before comparing, both the line
and every catalog wording are rewritten into one financial vocabulary (debt, loans and indebtedness are
borrowings; net income and net earnings are net profit; debtors are receivables; turnover is revenue...). The
score then combines the words they share and their spelling (character trigrams, which forgive OCR slips and
"amortisation"/"amortization").

It runs locally with no model and no new library: the bank's figures stay inside DPS. A match is a suggestion
for a person, or used on its own only when the borrower confirmed that wording before (borrower memory).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

# One financial vocabulary: (pattern, replacement), applied in order.
_REWRITES: list[tuple[str, str]] = [
    (r"&", " and "),
    (r"\bstock[- ]?in[- ]?trade\b|\bstocks\b|\binventory\b", "inventories"),
    (r"\bsundry debtors\b|\bdebtors\b|\baccounts receivable\b|\breceivable\b", "receivables"),
    (r"\bindebtedness\b|\bdebts?\b|\bloans?\b|\bborrowing\b|\bnotes payable\b|\bdebentures\b|\bbonds\b", "borrowings"),
    (r"\bsundry creditors\b|\bcreditors\b|\baccounts payable\b|\bpayable\b", "payables"),
    (r"\bturnover\b|\bnet sales\b|\bsales\b|\brevenues\b", "revenue"),
    (r"\bnet (income|earnings)\b|\bprofit after tax(ation)?\b|\bprofit for the (year|period)\b", "net profit"),
    (r"\b(income|earnings|profit) before (income )?tax(es|ation)?\b", "profit before tax"),
    (r"\bnon[- ]?current\b|\blong[- ]?term\b", "longterm"),
    (r"\bshort[- ]?term\b|\bcurrent maturities\b|\bcurrent portion\b", "shortterm"),
    (r"\binterest expense\b|\bfinance (charges|expenses?|costs?)\b|\binterest (cost|paid)\b", "finance costs"),
    (r"\bless\s+shortterm\b", "longterm"),  # "Notes payable, less current maturities" is the long-term part
    (r"\battributable to .*$", ""),  # "Profit for the year attributable to owners": the profit, not the owners
    (r"\bamortization\b", "amortisation"),
    (r"\bstockholders\b|\bshareholders'?\b|\bshare ?holders\b|\bowners\b", "shareholders"),
    (r"\bnet worth\b|\b(total )?shareholders'? (equity|funds?)\b|\btotal equity\b", "net worth"),
    (r"\bfixed assets\b|\bproperty,? plant,? and equipment\b|\bpp ?and ?e\b", "property plant equipment"),
    (r"\btrading activities\b|\boperations\b", "operating activities"),
    (r"\bcash and (bank|cash equivalents)( balances)?\b|\bcash equivalents\b", "cash"),
]
_STOP = {"the", "of", "and", "for", "from", "in", "on", "to", "a", "an", "total", "net", "less", "other", "as", "at",
         "by", "with", "its", "year", "period", "ended", "per", "note", "notes"}
_NUMBERING = re.compile(r"^\s*(?:\(?[a-z0-9]{1,4}[.)]\s*)+")


def canonical(label: str) -> str:
    text = _NUMBERING.sub("", (label or "").lower().replace("’", "'"))
    # "(net)", "(refer note 12)" say nothing about the line; "(non-current)" does, so its words stay.
    text = re.sub(r"\((?:[^)]*\b(?:note|refer|see|net)\b[^)]*|[^)]*\d[^)]*)\)", " ", text)
    for pattern, replacement in _REWRITES:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", text)).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in text.split() if w not in _STOP and len(w) > 2}


def _trigrams(text: str) -> Counter:
    t = f"  {text} "
    return Counter(t[i:i + 3] for i in range(len(t) - 2))


def _cosine(a: Counter, b: Counter) -> float:
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def similarity(a: str, b: str) -> float:
    """0..1: shared words (Dice) weighted 0.7, spelling (trigram cosine) 0.3, both after rewriting."""
    ca, cb = canonical(a), canonical(b)
    ta, tb = _tokens(ca), _tokens(cb)
    words = 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0
    return round(0.7 * words + 0.3 * _cosine(_trigrams(ca), _trigrams(cb)), 4)


@lru_cache(maxsize=1)
def _index() -> list[tuple[str, str]]:
    from dps.extraction.statement import LINE_ALIASES

    return [(key, phrase) for key, phrases in LINE_ALIASES.items() for phrase in phrases if len(phrase) > 3]


def nearest(label: str) -> tuple[str | None, float]:
    """The catalog line a label means most, with its score (None when nothing scores at all)."""
    best: tuple[str | None, float] = (None, 0.0)
    for key, phrase in _index():
        score = similarity(label, phrase)
        if score > best[1]:
            best = (key, score)
    return best
