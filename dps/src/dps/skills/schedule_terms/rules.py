"""schedule_terms pack rules: testing calendar, reporting deadlines, repayment, interest reset and cure windows."""

from __future__ import annotations

import re
from datetime import date

from dps.money import MONEY_MARKER, currency_after_amount, currency_before_amount, document_currencies
from dps.skills.common import amount, issue, line_at, make_field, scaled

PACK = "schedule_terms"
FREQ_WORDS = {"quarter": "QUARTERLY", "quarterly": "QUARTERLY", "half": "HALF_YEARLY", "half-year": "HALF_YEARLY",
              "half year": "HALF_YEARLY", "semi-annual": "HALF_YEARLY", "year": "YEARLY", "financial year": "YEARLY",
              "annual": "YEARLY", "annually": "YEARLY", "month": "MONTHLY", "monthly": "MONTHLY"}
DEADLINE = re.compile(r"within\s+(\d{1,3})\s*(?:\(\w+\)\s*)?days\s+(?:of|from|after)\s+(?:the\s+)?(?:end|close|expiry)?"
                      r"\s*(?:of\s+)?(?:each|every|the|its)?\s*(financial\s+year|half[\s-]*year|quarter|month|year)",
                      re.I)
TESTED = re.compile(r"\btested\s+(quarterly|half[\s-]*yearly|semi[\s-]*annually|annually|monthly|on\s+each\s+"
                    r"(quarter|half[\s-]*year|financial\s+year)\s+(?:end\s+)?date)", re.I)
REPAY = re.compile(r"\b(?:repa\w+\s+in|in)\s+(\d{1,3})\s+(?:equal\s+|structured\s+)?(monthly|quarterly|half[\s-]*yearly"
                   r"|annual|yearly)\s+instal?ments?(?:\s+of\s+(" + MONEY_MARKER + r")?\s*([\d,]+(?:\.\d+)?)\s*"
                   r"(crores?|lakhs?|millions?)?)?", re.I)
FIRST_DUE = re.compile(r"first\s+instal?ment[^\n.]{0,60}?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})",
                       re.I)
RESET = re.compile(r"\b(?:interest|rate|spread)[^\n.]{0,60}?\breset\b[^\n.]{0,80}", re.I)
CURE = re.compile(r"\b(?:cure|remedy|rectify)\w*\b[^\n.]{0,120}?within\s+(\d{1,3})\s*(?:\(\w+\)\s*)?(days|months)",
                  re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], start=1)}


def _freq(word: str) -> str | None:
    w = re.sub(r"[\s-]+", " ", word.lower()).replace("half yearly", "half").replace("semi annually", "half")
    for k, v in FREQ_WORDS.items():
        if k in w:
            return v
    return None


def _value(kind: str, rule: str, **kw) -> dict:
    return {"kind": kind, "rule": rule[:300], "frequency": kw.get("frequency"),
            "daysAfterPeriodEnd": kw.get("days"), "dates": kw.get("dates", []), "amount": kw.get("amount"),
            "currency": kw.get("currency")}


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    out, seen = [], set()
    local = document_currencies(" ".join(p.get("text") or "" for p in pages))  # "Rs." / "$" / "¥" of this document

    def add(kind, page, m, value, conf=0.85):
        key = (kind, value.get("frequency"), value.get("daysAfterPeriodEnd"), value.get("amount"))
        if key in seen:
            return
        seen.add(key)
        quote = line_at(page["text"], m.start(), m.end())
        out.append(make_field(PACK, pages, "SCHEDULE_TERM", kind, value, page["no"], quote, confidence=conf,
                              key=str(key)))

    for p in pages:
        text = p.get("text") or ""
        for m in DEADLINE.finditer(text):
            add("REPORTING_DEADLINE", p, m, _value("REPORTING_DEADLINE", m.group(0), frequency=_freq(m.group(2)),
                                                   days=int(m.group(1))))
        for m in TESTED.finditer(text):
            add("TESTING_CALENDAR", p, m, _value("TESTING_CALENDAR", m.group(0),
                                                 frequency=_freq(m.group(2) or m.group(1))))
        for m in REPAY.finditer(text):
            per = scaled(amount(m.group(4)), m.group(5)) if m.group(4) else None
            dates = []
            fd = FIRST_DUE.search(text)
            if fd and (mo := MONTHS.get(fd.group(2).lower()[:3])):
                try:
                    dates = [date(int(fd.group(3)), mo, int(fd.group(1))).isoformat()]
                except ValueError:
                    dates = []
            value = _value("REPAYMENT", m.group(0), frequency=_freq(m.group(2)), dates=dates,
                           amount=per * int(m.group(1)) if per else None,
                           currency=currency_before_amount(m.group(3), local) if m.group(3)
                           else currency_after_amount(text[m.end():m.end() + 60], local) if m.group(4) else None)
            value["instalments"] = int(m.group(1))
            add("REPAYMENT", p, m, value)
        for m in RESET.finditer(text):
            add("INTEREST_RESET", p, m, _value("INTEREST_RESET", m.group(0), frequency=_freq(m.group(0))), 0.7)
        for m in CURE.finditer(text):
            days = int(m.group(1)) * (30 if m.group(2).lower().startswith("month") else 1)
            add("CURE_WINDOW", p, m, _value("CURE_WINDOW", m.group(0), days=days))
    return out


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    warnings = []
    sanctioned = ctx.get("sanctionedAmount")
    for f in fields:
        v = f["value"]
        if f["name"] == "REPAYMENT" and sanctioned and v.get("amount"):
            if abs(v["amount"] - sanctioned) > 0.005 * sanctioned:
                f["issues"].append(issue("TOTALS_DO_NOT_ADD",
                                         f"instalments add to {v['amount']:,.0f}, sanctioned {sanctioned:,.0f}"))
                f["status"] = "NEEDS_REVIEW"
        tenor_end = ctx.get("tenorEnd")
        if tenor_end and any(d > tenor_end for d in v.get("dates") or []):
            f["issues"].append(issue("OUTSIDE_TENOR", "a date falls after the end of the facility tenor"))
            f["status"] = "NEEDS_REVIEW"
    return warnings
