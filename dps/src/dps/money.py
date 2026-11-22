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

# Currencies a document names: ISO code, symbol and name, case-insensitive. The one list every reader uses (Phase 1,
# 22 Nov 2026: eight readers kept their own lists of 2 to 12 currencies). "Rs." alone is the rupee of India, Sri
# Lanka, Pakistan or Nepal: it names no currency, and the loan's currency is used. A code that is also an English
# word (TRY, PHP, RUB) or a short symbol (RM, Rp, R$) counts only right before an amount.
_AMOUNT = r"(?=\s?[\d.,]*\d)"
_REGISTRY: list[tuple[str, tuple[str, ...]]] = [
    ("LKR", (r"\bLKR\b", r"\bSLRs?\b", r"\bSL\s?Rs\b\.?", r"Sri\s+Lank(?:a|an)\s+Rupees?")),
    ("INR", (r"\bINR\b", "₹", r"Indian\s+Rupees?")),
    ("PKR", (r"\bPKR\b", r"Pakistan(?:i)?\s+Rupees?")),
    ("NPR", (r"\bNPR\b", r"Nepal(?:ese)?\s+Rupees?")),
    ("MUR", (r"\bMUR\b", r"Mauritian\s+Rupees?")),
    ("BDT", (r"\bBDT\b", r"\bTaka\b")),
    ("USD", (r"\bUSD\b", r"US\$", r"\bU\.?S\.?\s+Dollars?", r"United\s+States\s+Dollars?")),
    ("AED", (r"\bAED\b", r"\bDirhams?\b")),
    ("SAR", (r"\bSAR\b", r"Saudi\s+Riyals?")),
    ("QAR", (r"\bQAR\b", r"Qatari?\s+Riyals?")),
    ("KWD", (r"\bKWD\b", r"Kuwaiti\s+Dinars?")),
    ("OMR", (r"\bOMR\b", r"Omani\s+Rials?")),
    ("BHD", (r"\bBHD\b", r"Bahraini\s+Dinars?")),
    ("GBP", (r"\bGBP\b", "£", r"Pounds?\s+Sterling")),
    ("EGP", (r"\bEGP\b", r"Egyptian\s+Pounds?")),
    ("EUR", (r"\bEUR\b", "€", r"\bEuros?\b")),
    ("SGD", (r"\bSGD\b", r"\bS\$", r"Singapore\s+Dollars?")),
    ("HKD", (r"\bHKD\b", r"\bHK\$", r"Hong\s+Kong\s+Dollars?")),
    ("AUD", (r"\bAUD\b", r"\bAU?\$", r"Australian\s+Dollars?")),
    ("CAD", (r"\bCAD\b", r"\bCA?\$", r"Canadian\s+Dollars?")),
    ("NZD", (r"\bNZD\b", r"\bNZ\$", r"New\s+Zealand\s+Dollars?")),
    ("JPY", (r"\bJPY\b", "¥(?!\\s*RMB)", r"Japanese\s+Yen", r"\bYen\b")),
    ("CNY", (r"\bCNY\b", r"\bRMB\b", r"CN¥", r"\bRenminbi\b", r"Chinese\s+Yuan")),
    ("CHF", (r"\bCHF\b", r"Swiss\s+Francs?")),
    ("ZAR", (r"\bZAR\b", r"South\s+African\s+Rand")),
    ("MYR", (r"\bMYR\b", r"\bRM" + _AMOUNT, r"Malaysian\s+Ringgit", r"\bRinggit\b")),
    ("IDR", (r"\bIDR\b", r"\bRp\.?" + _AMOUNT, r"Indonesian\s+Rupiah", r"\bRupiah\b")),
    ("THB", (r"\bTHB\b", "฿", r"Thai\s+Baht", r"\bBaht\b")),
    ("PHP", (r"\bPHP" + _AMOUNT, "₱", r"Philippine\s+Pesos?")),
    ("KRW", (r"\bKRW\b", "₩", r"Korean\s+Won")),
    ("SEK", (r"\bSEK\b", r"Swedish\s+Kron(?:a|or)")),
    ("NOK", (r"\bNOK\b", r"Norwegian\s+Kron(?:e|er)")),
    ("DKK", (r"\bDKK\b", r"Danish\s+Kron(?:e|er)")),
    ("BRL", (r"\bBRL\b", r"\bR\$", r"Brazilian\s+Rea(?:l|is)")),
    ("MXN", (r"\bMXN\b", r"\bMX\$", r"Mexican\s+Pesos?")),
    ("KES", (r"\bKES\b", r"\bKSh", r"Kenyan\s+Shillings?")),
    ("NGN", (r"\bNGN\b", "₦", r"Nigerian\s+Naira", r"\bNaira\b")),
    ("VND", (r"\bVND\b", "₫", r"Vietnamese\s+Dong")),
    ("ILS", (r"\bILS\b", "₪", r"Israeli\s+(?:New\s+)?Shekels?")),
    ("PLN", (r"\bPLN\b", r"Polish\s+Zloty")),
    ("CZK", (r"\bCZK\b", r"Czech\s+Korun[ay]")),
    ("HUF", (r"\bHUF\b", r"Hungarian\s+Forints?")),
    ("TRY", (r"\bTRY" + _AMOUNT, "₺", r"Turkish\s+Lira")),
    ("RUB", (r"\bRUB" + _AMOUNT, "₽", r"Russian\s+Roubles?", r"Russian\s+Rubles?")),
]
CODES: tuple[str, ...] = tuple(code for code, _ in _REGISTRY)
_CURRENCY_WORDS: list[tuple[str, re.Pattern]] = [(code, re.compile("|".join(marks), re.I)) for code, marks in _REGISTRY]
# What may stand before an amount: every currency above, and the symbols several countries share ("Rs.", "$", "¥",
# "kr", "R", "Fr."; see SHARED_SYMBOLS). A "$" with letters before it ("NT$", "B$", "J$") is a dollar not in the list:
# it is no marker, so its amount gets no currency rather than a wrong one. "kr", "R" and "Fr." are so short that a
# label ("R1", "Schedule R 5") could look like money: they count only as written (capital R, Fr) and only before a
# real amount (thousands separators, four or more digits, or a scale word).
_REAL_AMOUNT = (r"(?=\s?(?:\d{1,3}(?:[,.\s]\d{3})+|\d{4,}|\d+(?:\.\d+)?\s*(?:million|mn|billion|bn|thousand|m)\b))")
_LOOSE = (r"\bRs\.?", r"(?:(?:Sri\s+Lankan?|Indian|Pakistani?|Nepalese|Nepal)\s+)?\bRupees?\b",
          r"(?<![A-Za-z])\$", r"(?-i:\b[Kk]r)\.?" + _REAL_AMOUNT, r"(?-i:\bR)" + _REAL_AMOUNT,
          r"(?-i:\bFr)\.?" + _REAL_AMOUNT)
MONEY_MARKER = "(?:" + "|".join([m for _, marks in _REGISTRY for m in marks] + list(_LOOSE)) + ")"
_BEFORE_AMOUNT = re.compile(MONEY_MARKER, re.I)
# A currency written after the amount ("500 million Sri Lankan Rupees", "5,000,000 euros"): its code or name (not a
# symbol), after an optional scale word.
_WORD_MARKS = [m for _, marks in _REGISTRY for m in marks if re.match(r"(?:\\b)?[A-Za-z]", m)]
_AFTER_AMOUNT = re.compile(r"\s*(?:(?:million|mn|billion|bn|crores?|cr|lakhs?|thousand)\b\.?\s*)?(?:of\s+)?(?:"
                           + "|".join(_WORD_MARKS) + r"|\bRupees?\b)", re.I)
# For readers' own patterns: "a currency is named right after this point" (a lookahead, it takes no text).
CURRENCY_AFTER = "(?=" + _AFTER_AMOUNT.pattern + ")"
# For readers that list currency codes inside their own patterns ("amounts in JPY millions").
CODE_WORDS = r"\b(?:" + "|".join(c.lower() for c in CODES) + r")\b"


def detect_currency(text: str | None, extra: list[tuple[str, re.Pattern]] | None = None) -> str | None:
    """The currency a text names first, or None when it names none (or only "Rs."). ``extra``: the words of the
    bank's country profiles (dps.profiles.currency_words), so a country added in Core is recognised too."""
    best: tuple[int, str] | None = None
    for code, pattern in [*_CURRENCY_WORDS, *(extra or [])]:
        m = pattern.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), code)
    return best[1] if best else None


# A bare symbol several countries share: "Rs." (India, Sri Lanka, Pakistan, Nepal, Mauritius), "$" (the US,
# Australia, Canada, Singapore, Hong Kong, New Zealand, Mexico), "¥" (Japan, China), "kr" (Sweden, Norway,
# Denmark), "R" (South Africa) and "Fr." (Switzerland). It stands for the currency of that family the document
# itself names most ("Sri Lankan Rupees ... Rs. 500 mn" is LKR; "AUD 150,000,000 ... $5,000,000" is AUD). When the
# document names none of them it stands for the default: INR, USD and JPY by long habit; none for "kr", "R" and
# "Fr.", which are too short to be sure of on their own (22 Nov 2026). DPS never guesses a currency it cannot see.
SHARED_SYMBOLS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "Rs": ("INR", ("INR", "LKR", "PKR", "NPR", "MUR")),
    "$": ("USD", ("USD", "AUD", "CAD", "SGD", "HKD", "NZD", "MXN")),
    "¥": ("JPY", ("JPY", "CNY")),
    "kr": (None, ("SEK", "NOK", "DKK")),
    "R": (None, ("ZAR",)),
    "Fr": (None, ("CHF",)),
}
RUPEES = SHARED_SYMBOLS["Rs"][1]
# '"Dollars" and "$" mean lawful money of Canada' settles it, whatever else the document names.
_DEFINED_AS = re.compile(r"[\"“](?:\$|Dollars?|¥|Yen)[\"”][^.;]{0,120}?lawful\s+(?:money|currency)\s+of\s+(?:the\s+)?"
                         r"(United\s+States|Canada|Australia|Singapore|Hong\s+Kong|New\s+Zealand|Mexico|Japan|"
                         r"(?:People'?s\s+Republic\s+of\s+)?China)", re.I)
_COUNTRY = {"united states": "USD", "canada": "CAD", "australia": "AUD", "singapore": "SGD", "hong kong": "HKD",
            "new zealand": "NZD", "mexico": "MXN", "japan": "JPY", "china": "CNY"}


def document_currencies(text: str | None) -> dict[str, str | None]:
    """What each shared symbol stands for in this document: {"Rs": "LKR", "$": "AUD", "¥": "JPY", "kr": None, ...}."""
    text = text or ""
    words = dict(_CURRENCY_WORDS)
    out: dict[str, str | None] = {}
    for symbol, (default, family) in SHARED_SYMBOLS.items():
        counts = {code: sum(1 for m in words[code].finditer(text) if m.group(0).strip() not in ("$", "¥"))
                  for code in family}
        best = max(family, key=lambda code: (counts[code], code == default))
        out[symbol] = best if counts[best] else default
    for m in _DEFINED_AS.finditer(text):
        code = _COUNTRY[re.sub(r"\s+", " ", m.group(1).lower()).split("republic of ")[-1]]
        out["¥" if code in SHARED_SYMBOLS["¥"][1] else "$"] = code
    return out


def document_rupee(text: str | None) -> str:
    """The rupee a document names ("Sri Lankan Rupees", "LKR"), for its bare "Rs."; INR when it names none."""
    return document_currencies(text)["Rs"] or "INR"


def _shared(word: str) -> str | None:
    """Which shared symbol a marker is ("Rs." -> "Rs", "kr." -> "kr"), or None for one that names its currency."""
    w = word.strip().rstrip(".").lower()
    if w in ("$", "¥"):
        return w
    if w in ("rs", "rupee", "rupees"):
        return "Rs"
    return {"kr": "kr", "r": "R", "fr": "Fr"}.get(w)


def currency_before_amount(marker: str | None, local: dict[str, str | None] | str | None = None) -> str | None:
    """The currency a marker written before an amount stands for ("HK$" HKD, "LKR" LKR), or None. A shared symbol
    ("Rs.", "$", "¥", "kr", "R", "Fr.") stands for what ``local`` (document_currencies of the document) says, else its
    default (INR, USD, JPY; none for the others). A plain string is the document's rupee (older callers). A currency
    not in the list gives None, never another currency."""
    m = _BEFORE_AMOUNT.search(marker or "")
    if not m:
        return None
    local = {"Rs": local} if isinstance(local, str) else (local or {})
    shared = _shared(m.group(0))
    if shared:
        return local.get(shared, SHARED_SYMBOLS[shared][0])
    code = detect_currency(marker[m.start():])
    if code:
        return code
    return "INR" if m.group(0).strip().lower().startswith("indian") else None


def currency_after_amount(after: str | None, local: dict[str, str | None] | str | None = None) -> str | None:
    """The currency named right after an amount ("500 million Sri Lankan Rupees" -> LKR, "5,000,000 euros" -> EUR),
    or None. ``after``: the text that follows the number."""
    m = _AFTER_AMOUNT.match(after or "")
    if not m:
        return None
    code = detect_currency(m.group(0))
    if code:
        return code
    local = {"Rs": local} if isinstance(local, str) else (local or {})
    return local.get("Rs", "INR")  # "500 million Rupees": the document's rupee


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


def currency_words() -> list[tuple[str, re.Pattern]]:
    """Every currency of the list, as (ISO code, pattern), for readers that count which currency a document uses."""
    return list(_CURRENCY_WORDS)
