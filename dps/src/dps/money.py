"""Money for any country (Phase 1, 10 November 2026).

Every amount was turned into Indian crore and labelled INR, so a Sri Lankan or UAE statement's figures were shown
as rupees of India and an agreement's "LKR 8,000 Mn" was held for a person. Now each loan has a currency, and an
amount is kept in that currency's *money unit*: crore for the Indian rupee (as before, so nothing stored changes)
and millions for every other currency (the international habit: "LKR 800 Mn", "AED 1.2 bn").
"""

from __future__ import annotations

import re

# How many units of a currency one printed scale is ("Rs. '000" is thousand, "Mn" million, "Cr" crore).
SCALE_UNITS: dict[str | None, float] = {
    "rupee": 1.0, "unit": 1.0, "hundred": 1e2, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
    "million": 1e6, "mn": 1e6, "crore": 1e7, "crores": 1e7, "cr": 1e7, "billion": 1e9, "bn": 1e9,
}

# Currencies a statement or an agreement names. "Rs." alone is the rupee of India, Sri Lanka, Pakistan or Nepal:
# it names no currency, and the loan's currency is used.
_CURRENCY_WORDS: list[tuple[str, re.Pattern]] = [
    ("LKR", re.compile(r"\bLKR\b|\bSLRs?\b|Sri\s+Lank(?:a|an)\s+Rupees?", re.I)),
    ("INR", re.compile(r"\bINR\b|₹|Indian\s+Rupees?", re.I)),
    ("PKR", re.compile(r"\bPKR\b|Pakistan(?:i)?\s+Rupees?", re.I)),
    ("NPR", re.compile(r"\bNPR\b|Nepal(?:ese)?\s+Rupees?", re.I)),
    ("BDT", re.compile(r"\bBDT\b|\bTaka\b", re.I)),
    ("USD", re.compile(r"\bUSD\b|US\$|\bU\.?S\.?\s+Dollars?|United\s+States\s+Dollars?", re.I)),
    ("AED", re.compile(r"\bAED\b|\bDirhams?\b", re.I)),
    ("SAR", re.compile(r"\bSAR\b|Saudi\s+Riyals?", re.I)),
    ("QAR", re.compile(r"\bQAR\b|Qatari?\s+Riyals?", re.I)),
    ("GBP", re.compile(r"\bGBP\b|£|Pounds?\s+Sterling", re.I)),
    ("EUR", re.compile(r"\bEUR\b|€|\bEuros?\b", re.I)),
    ("SGD", re.compile(r"\bSGD\b|\bS\$", re.I)),
]


def detect_currency(text: str | None, extra: list[tuple[str, re.Pattern]] | None = None) -> str | None:
    """The currency a text names first, or None when it names none (or only "Rs."). ``extra``: the words of the
    bank's country profiles (dps.profiles.currency_words), so a country added in Core is recognised too."""
    best: tuple[int, str] | None = None
    for code, pattern in [*_CURRENCY_WORDS, *(extra or [])]:
        m = pattern.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), code)
    return best[1] if best else None


def money_unit(currency: str) -> str:
    """The unit amounts in this currency are kept in: INR_CR for the Indian rupee, <CODE>_MN for any other."""
    return "INR_CR" if currency == "INR" else f"{currency}_MN"


def unit_size(currency: str) -> float:
    """Currency units in one money unit: a crore (10^7) for INR, a million for any other."""
    return 1e7 if currency == "INR" else 1e6


def currency_of(unit: str | None) -> str | None:
    """"LKR_MN" -> "LKR"; "INR_CR" -> "INR"; None for a unit that is no money unit ("x", "PERCENT")."""
    m = re.fullmatch(r"([A-Z]{3})_(?:MN|CR)", unit or "")
    return m.group(1) if m else None


def to_money_unit(value: float, scale: str | None, currency: str, rate: float = 1.0) -> float:
    """A figure printed in ``scale`` of some currency, as ``currency``'s money unit (``rate`` converts between)."""
    return value * SCALE_UNITS[scale or "unit"] * rate / unit_size(currency)


def describe(value: float, unit: str) -> str:
    """"LKR 800 Mn", "INR 4,500 cr": an amount in its money unit, for notes and messages."""
    ccy = currency_of(unit)
    if ccy is None:
        return f"{value:,.2f} {unit}"
    return f"{ccy} {value:,.2f} {'cr' if ccy == 'INR' else 'Mn'}"
