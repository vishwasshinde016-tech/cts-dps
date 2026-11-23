"""sanction pack rules: sanctioned amount, facility type, tenor, rate, security, conditions precedent, validity,
plus the covenants in the terms table (the covenants pack's extractor)."""

from __future__ import annotations

import re

from dps.money import CURRENCY_AFTER, MONEY_MARKER, currency_after_amount, currency_before_amount, document_currencies
from dps.skills.common import amount, issue, line_at, make_field, scaled
from dps.skills.covenants.rules import as_pages, term_field

PACK = "sanction"
CUR = "(" + MONEY_MARKER + ")"
AMOUNT = re.compile(r"(sanction(?:ed)?\s+(?:limit|amount)|(?:facility|loan|limit)\s+amount|amount\s+of\s+(?:the\s+)?"
                    r"(?:facility|loan)|limit)\s*[:\-]?\s*[^\n\d]{0,30}?" + CUR + r"\s*([\d,]+(?:\.\d+)?)\s*"
                    r"(crores?|cr\.?|lakhs?|lacs?|millions?|mn)?", re.I)
# "Sanctioned Limit: 500 million Sri Lankan Rupees": the currency after the amount (group 2 stays empty)
AMOUNT_AFTER = re.compile(AMOUNT.pattern.split("[^\\n\\d]{0,30}?")[0] + r"[^\n\d]{0,30}?()([\d,]+(?:\.\d+)?)\s*"
                          r"(crores?|cr\.?|lakhs?|lacs?|millions?|mn)?" + CURRENCY_AFTER, re.I)
FACILITY = re.compile(r"\b(rupee\s+term\s+loan|term\s+loan|cash\s+credit|working\s+capital\s+demand\s+loan|"
                      r"working\s+capital(?:\s+limit)?|overdraft|letter\s+of\s+credit|bank\s+guarantee|"
                      r"external\s+commercial\s+borrowing|bill\s+discounting|packing\s+credit)\b", re.I)
TENOR = re.compile(r"\b(tenor|tenure|repayment\s+period|door[\s-]+to[\s-]+door)\b[^\n\d]{0,40}?(\d{1,3})\s*"
                   r"(months?|years?)", re.I)
RATE = re.compile(r"\b(rate\s+of\s+interest|interest\s+rate|roi|pricing)\b[^\n%]{0,80}?(\d{1,2}(?:\.\d{1,2})?)\s*%"
                  r"([^\n]{0,60})", re.I)
SECURITY = re.compile(r"^.*\b(primary\s+security|collateral\s+security|security\s*:|hypothecation\s+of|"
                      r"equitable\s+mortgage|personal\s+guarantee|corporate\s+guarantee)\b.*$", re.I | re.M)
CP = re.compile(r"\bconditions?\s+precedent\b", re.I)
VALIDITY = re.compile(r"\b(?:this\s+)?sanction\s+(?:is\s+)?valid\s+(?:for|till|up\s*to|until)\s+([^\n.]{3,60})", re.I)
CURRENCY = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "usd": "USD", "us$": "USD", "lkr": "LKR",
            "eur": "EUR"}


def _first(pages, pattern):
    for p in pages:
        m = pattern.search(p.get("text") or "")
        if m:
            return p, m
    return None, None


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    out = []
    local = document_currencies(" ".join(q.get("text") or "" for q in pages))  # "Rs." / "$" of this document
    p, m = _first(pages, AMOUNT)
    if not m:
        p, m = _first(pages, AMOUNT_AFTER)
    if m:
        value = scaled(amount(m.group(3)), m.group(4))
        currency = (currency_before_amount(m.group(2), local) or CURRENCY.get(m.group(2).lower(), m.group(2).upper())
                    if m.group(2) else currency_after_amount(m.string[m.end():m.end() + 60], local))
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "SANCTIONED_AMOUNT",
                              {"key": "SANCTIONED_AMOUNT", "text": m.group(0).strip()[:200], "amount": value,
                               "currency": currency, "date": None},
                              p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.9))
    p, m = _first(pages, FACILITY)
    if m:
        kind = re.sub(r"\s+", "_", m.group(1).upper())
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "FACILITY_TYPE",
                              {"key": "FACILITY_TYPE", "text": kind, "amount": None, "currency": None, "date": None},
                              p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.85))
    p, m = _first(pages, TENOR)
    if m:
        months = int(m.group(2)) * (12 if m.group(3).lower().startswith("year") else 1)
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "TENOR_MONTHS",
                              {"key": "TENOR_MONTHS", "text": m.group(0).strip()[:200], "amount": float(months),
                               "currency": None, "date": None},
                              p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.85))
    p, m = _first(pages, RATE)
    if m:
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "INTEREST_RATE",
                              {"key": "INTEREST_RATE", "text": (m.group(2) + "%" + m.group(3)).strip()[:200],
                               "amount": float(m.group(2)), "currency": None, "date": None},
                              p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.85))
    for p in pages:
        for m in SECURITY.finditer(p.get("text") or ""):
            out.append(make_field(PACK, pages, "AGREEMENT_TERM", "SECURITY",
                                  {"key": "SECURITY", "text": m.group(0).strip()[:300], "amount": None,
                                   "currency": None, "date": None},
                                  p["no"], m.group(0).strip(), confidence=0.8, key=str(m.start())))
    p, m = _first(pages, CP)
    if m:
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "CONDITIONS_PRECEDENT",
                              {"key": "CONDITIONS_PRECEDENT", "text": p["text"][m.start():m.start() + 600].strip(),
                               "amount": None, "currency": None, "date": None},
                              p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.75))
    p, m = _first(pages, VALIDITY)
    if m:
        out.append(make_field(PACK, pages, "AGREEMENT_TERM", "VALIDITY",
                              {"key": "VALIDITY", "text": m.group(1).strip(), "amount": None, "currency": None,
                               "date": None}, p["no"], line_at(p["text"], m.start(), m.end()), confidence=0.8))
    from dps.extraction.agreement import extract_terms

    out.extend(term_field(PACK, pages, t) for t in extract_terms(as_pages(pages)) if t.category != "INFO")
    return out


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    names = {f["name"] for f in fields}
    warnings = []
    for key in ("SANCTIONED_AMOUNT", "TENOR_MONTHS"):
        if key not in names:
            warnings.append(issue("NOTHING_FOUND", f"{key} not found in the sanction letter"))
    return warnings
