"""Identifiers (Phase 3): what borrower, facility, period, currency and units a document names, with evidence.

Every candidate is found by pattern, checked by its validator (PAN format, GSTIN check digit, LEI ISO 17442
mod-97, CIN, UDIN, real calendar dates) and then matched against the bank's master data through the Spring
lookup tool (``spring_tools.lookup_identifiers``). DPS never assigns a document: it reports the match status
(EXACT, STRONG, FUZZY, CONFLICT, NONE) and the DMS applies the mapping precedence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PAN = re.compile(r"\b([A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z])\b")
GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
CIN = re.compile(r"\b([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b")
LEI = re.compile(r"\b([0-9A-Z]{18}\d{2})\b")
UDIN = re.compile(r"\bUDIN\s*[:\-]?\s*([0-9]{8}[0-9A-Z]{10})\b", re.I)
LOAN_ACCOUNT = re.compile(r"\b(?:loan\s+)?(?:a/?c|account)\s*(?:no\.?|number|#)\s*[:\-]?\s*([0-9][0-9\- ]{8,22}[0-9])",
                          re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
PERIOD_END = re.compile(
    r"\b(year|quarter|half[\s-]+year|period|(?:three|six|nine|twelve)\s+months|month)\s+ended\s+(?:on\s+)?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})", re.I)
AS_AT = re.compile(r"\bas\s+(?:at|on)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})", re.I)
CURRENCY = [(re.compile(r"₹|\bINR\b|\bRs\.?(?=\s|\d)|\brupees?\b", re.I), "INR"),
            (re.compile(r"\bUSD\b|US\$|\$(?=\s?\d)"), "USD"), (re.compile(r"\bLKR\b|\bSLR\b"), "LKR"),
            (re.compile(r"\bEUR\b|€"), "EUR"), (re.compile(r"\bGBP\b|£"), "GBP"),
            (re.compile(r"\bAED\b|\bdirhams?\b", re.I), "AED")]
UNIT = re.compile(r"\(?\s*(?:₹|rs\.?|inr|usd|amounts?)?\s*(?:in|all\s+amounts\s+in)\s+(?:₹|rs\.?|inr|usd)?\s*"
                  r"(lakhs?|lacs?|crores?|thousands?|millions?|billions?|'000)\b", re.I)
UNIT_NAMES = {"lakh": "LAKH", "lac": "LAKH", "crore": "CRORE", "thousand": "THOUSAND", "'000": "THOUSAND",
              "million": "MILLION", "billion": "BILLION"}
# G3 (spec: "document issuer/source"): who issued the document. Lender in an agreement, the bank on a sanction
# letter, the audit or CA firm on statements and certificates.
ORG = (r"([A-Z][A-Za-z0-9&.,'\- ]{2,80}?(?:Limited|Ltd\.?|LLP|Bank|Co\.?|& Co|Associates|Accountants)\b"
       r"(?:\s+(?:Limited|Ltd\.?))?)")
ISSUER = [
    ("LENDER", re.compile(r"^\s*lender\s*:\s*" + ORG, re.I | re.M)),
    ("LENDER", re.compile(ORG + r"\s*,?[^()\n]{0,160}\(\s*(?:hereinafter\s+(?:referred\s+to|called)\s+as\s+)?"
                          r"(?:the\s+)?[\"“]?(?:Lender|Bank)\b", re.I | re.S)),
    ("AUDITOR", re.compile(r"\bfor\s+" + ORG + r"\s*,?\s*\n?\s*Chartered\s+Accountants", re.I)),
    ("AUDITOR", re.compile(ORG + r"\s*\n?\s*Chartered\s+Accountants", re.I)),
    ("BANK", re.compile(r"^\s*([A-Z][A-Za-z&.' ]{2,60}\bBank(?:\s+(?:Limited|Ltd\.?))?)\s*$", re.M)),
]
# G3 (spec: "required fields"): what each document type must show. Missing ones are reported, never guessed.
REQUIRED = {
    "LOAN_AGREEMENT": ("BORROWER_NAME", "ISSUER", "SIGNATURE"),
    "SANCTION_LETTER": ("BORROWER_NAME", "ISSUER"),
    "AMENDMENT": ("ISSUER", "SIGNATURE"),
    "FINANCIAL_STATEMENT": ("PERIOD_END", "CURRENCY", "UNIT"),
    "COMPLIANCE_CERTIFICATE": ("PERIOD_END", "UDIN", "SIGNATURE"),
    "AMENDED_AND_RESTATED": ("BORROWER_NAME", "ISSUER", "SIGNATURE"),
    "GUARANTEE": ("ISSUER", "SIGNATURE"),
    "WAIVER_CONSENT": ("ISSUER", "SIGNATURE"),
}
BORROWER = [
    re.compile(r"\bM/s\.?\s+([A-Z][A-Za-z0-9&.,'()\- ]{2,80}?(?:Limited|Ltd\.?|LLP|Private Limited|Pvt\.? Ltd\.?))",
               re.M),
    re.compile(r"([A-Z][A-Za-z0-9&.,'\- ]{2,80}?(?:Limited|Ltd\.?|LLP|PLC|Inc\.?))\s*,?\s*\(?[^()\n]{0,120}"
               r"\(\s*(?:hereinafter\s+(?:referred\s+to|called)\s+as\s+)?(?:the\s+)?[\"“]?Borrower", re.I | re.S),
    re.compile(r"to\s+the\s+(?:members|board\s+of\s+directors)\s+of\s+([A-Z][A-Za-z0-9&.,'\- ]{2,80}?"
               r"(?:Limited|Ltd\.?|LLP))", re.I),
]
SIGNATURE = re.compile(r"\b(authori[sz]ed\s+signatory|for\s+and\s+on\s+behalf\s+of|signed\s+(?:and\s+delivered\s+)?"
                       r"by|signature\s*:|\(signature\)|managing\s+director|partner\s*\n|membership\s+no)", re.I)
LEGAL_SUFFIX = re.compile(r"\b(private|pvt|limited|ltd|llp|plc|inc|m/s|the)\b\.?", re.I)


def pan_valid(v: str) -> bool:
    return bool(PAN.fullmatch(v))


def gstin_valid(v: str) -> bool:
    if not GSTIN.fullmatch(v):
        return False
    total = 0
    for i, ch in enumerate(v[:14]):
        product = _B36.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _B36[(36 - total % 36) % 36] == v[14]


def lei_valid(v: str) -> bool:
    if not re.fullmatch(r"[0-9A-Z]{18}\d{2}", v):
        return False
    return int("".join(str(_B36.index(c)) for c in v)) % 97 == 1


def cin_valid(v: str) -> bool:
    if not CIN.fullmatch(v):
        return False
    return 1850 <= int(v[8:12]) <= date.today().year


def udin_valid(v: str) -> bool:
    return bool(re.fullmatch(r"\d{8}[0-9A-Z]{10}", v))


def normalise_name(name: str) -> str:
    cleaned = LEGAL_SUFFIX.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def name_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return round(SequenceMatcher(None, normalise_name(a), normalise_name(b)).ratio(), 3)


def _date(day: str, month: str, year: str) -> date | None:
    m = MONTHS.get(month.lower()[:9]) or MONTHS.get(month.lower()[:3])
    if not m:
        return None
    try:
        return date(int(year), m, int(day))
    except ValueError:
        return None


@dataclass
class Found:
    kind: str
    value: str
    page: int | None
    quote: str
    block_id: str | None = None
    bbox: list | None = None
    extra: dict = field(default_factory=dict)

    def as_result(self, status: str = "NONE", candidates: list[str] | None = None) -> dict:
        out = {"kind": self.kind, "value": self.value, **({"role": self.extra["role"]} if "role" in self.extra else {}),
               "source": {"page": self.page, "quote": self.quote[:300], "bbox": self.bbox, "blockId": self.block_id,
                          "cellRef": None},
               "match": {"status": status, "candidateIds": candidates or []}}
        return out


def _line(text: str, start: int, end: int) -> str:
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a:b if b != -1 else len(text)].strip()


def _block_for(page: dict, quote: str) -> tuple[str | None, list | None]:
    for b in page.get("blocks", []):
        if quote and (quote in b.get("text", "") or b.get("text", "") in quote) and b.get("text"):
            return b.get("id"), b.get("bbox")
    return None, None


def find_identifiers(model: dict, loan_account_pattern: str | None = None,
                     facility_pattern: str | None = None) -> list[Found]:
    found: list[Found] = []
    seen: set[tuple[str, str]] = set()
    pages = model.get("pages", [])
    account = re.compile(loan_account_pattern) if loan_account_pattern else LOAN_ACCOUNT
    facility = re.compile(facility_pattern) if facility_pattern else None

    def add(kind: str, value: str, page: dict, m: re.Match, **extra) -> None:
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        quote = _line(page.get("text") or "", m.start(), m.end())
        block, bbox = _block_for(page, quote)
        found.append(Found(kind, value, page["no"], quote, block, bbox, extra))

    validators = [("PAN", PAN, pan_valid), ("GSTIN", GSTIN, gstin_valid), ("CIN", CIN, cin_valid),
                  ("LEI", LEI, lei_valid)]
    for page in pages:
        text = page.get("text") or ""
        for kind, pattern, ok in validators:
            for m in pattern.finditer(text):
                if ok(m.group(1)):
                    add(kind, m.group(1), page, m)
        for m in UDIN.finditer(text):
            if udin_valid(m.group(1).upper()):
                add("UDIN", m.group(1).upper(), page, m)
        for m in account.finditer(text):
            digits = re.sub(r"[\s\-]", "", m.group(1) if m.groups() else m.group(0))
            if 8 <= len(digits) <= 20:
                add("LOAN_ACCOUNT", digits, page, m)
        if facility:
            for m in facility.finditer(text):
                add("FACILITY_ID", m.group(1) if m.groups() else m.group(0), page, m)
        for m in PERIOD_END.finditer(text):
            d = _date(m.group(2), m.group(3), m.group(4))
            if d:
                add("PERIOD_END", d.isoformat(), page, m, periodWord=m.group(1).lower())
        for m in UNIT.finditer(text):
            unit = next((v for k, v in UNIT_NAMES.items() if m.group(1).lower().startswith(k.rstrip("s"))), None)
            if unit:
                add("UNIT", unit, page, m)
        for pattern in BORROWER:
            for m in pattern.finditer(text):
                name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
                if len(normalise_name(name)) >= 3:
                    add("BORROWER_NAME", name, page, m)
    if not any(f.kind == "PERIOD_END" for f in found):
        for page in pages[:3]:
            m = AS_AT.search(page.get("text") or "")
            if m and (d := _date(m.group(1), m.group(2), m.group(3))):
                add("PERIOD_END", d.isoformat(), page, m)
                break
    found.extend(_currency(pages))
    issuer = _issuer(pages)
    if issuer:
        found.append(issuer)
    sig = _signature(pages)
    if sig:
        found.append(sig)
    return found


def _currency(pages: list[dict]) -> list[Found]:
    counts: dict[str, tuple[int, int, str]] = {}
    for page in pages[:10]:
        text = page.get("text") or ""
        for pattern, code in CURRENCY:
            hits = list(pattern.finditer(text))
            if hits:
                n, first_page, quote = counts.get(code, (0, page["no"], _line(text, hits[0].start(), hits[0].end())))
                counts[code] = (n + len(hits), first_page, quote)
    if not counts:
        return []
    code, (_, page_no, quote) = max(counts.items(), key=lambda kv: kv[1][0])
    return [Found("CURRENCY", code, page_no, quote)]


def _issuer(pages: list[dict]) -> Found | None:
    for role, pattern in ISSUER:
        for page in pages[:3] + pages[-2:]:
            text = page.get("text") or ""
            m = pattern.search(text)
            if m:
                name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
                if len(normalise_name(name)) >= 3:
                    return Found("ISSUER", name, page["no"], _line(text, m.start(), m.end()), extra={"role": role})
    return None


def missing_required(doc_type: str, found: list[Found]) -> list[str]:
    """G3: the fields this document type must show that were not found (an unsure signature counts as missing)."""
    have = {f.kind for f in found if not (f.kind == "SIGNATURE" and f.value != "PRESENT")}
    return [k for k in REQUIRED.get(doc_type, ()) if k not in have]


def _signature(pages: list[dict]) -> Found | None:
    for page in reversed(pages[-2:]):
        text = page.get("text") or ""
        m = SIGNATURE.search(text)
        if m:
            return Found("SIGNATURE", "PRESENT", page["no"], _line(text, m.start(), m.end()))
    if pages:
        return Found("SIGNATURE", "UNSURE", pages[-1]["no"], "")
    return None


def legibility(model: dict) -> float:
    pages = model.get("pages", [])
    if not pages:
        return 0.0
    return round(sum(float(p.get("quality", 0)) for p in pages) / len(pages), 3)


MATCHABLE = {"FACILITY_ID", "LOAN_ACCOUNT", "CIN", "PAN", "GSTIN", "LEI", "BORROWER_NAME"}


def match(found: list[Found], tools, context: dict | None,
          correlation_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """Identifier results with match status from Spring's lookup tool; warnings when the tool is unavailable."""
    items = [(str(i), f.kind, f.value) for i, f in enumerate(found) if f.kind in MATCHABLE]
    answers = tools.lookup_identifiers(items, context, correlation_id) if (tools is not None and items) else {}
    warnings: list[dict] = []
    out = []
    for i, f in enumerate(found):
        a = answers.get(str(i))
        if a is None:
            out.append(f.as_result("NONE"))
            continue
        if not a.available:
            if not warnings:
                warnings.append({"code": "SPRING_UNAVAILABLE",
                                 "message": "Master-data lookup unavailable; identifiers not matched."})
            out.append(f.as_result("NONE"))
            continue
        out.append(f.as_result(a.status or "NONE", [str(c.get("entityId")) for c in a.candidates]))
    return out, warnings
