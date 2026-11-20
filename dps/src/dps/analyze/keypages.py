"""Key pages (spec, structural validation: "determine ... key pages").

For each document type, the pages a reviewer and the extractors need: parties, definitions, the financial covenants,
the schedule, the signature page, the statements, the certificate body. Rules on each page's words only; a role that
is not found is left out (never guessed). Every entry carries the page number and the line that matched, as proof.
"""

from __future__ import annotations

import re

ROLES = {
    "PARTIES": re.compile(r"\b(?:by\s+and\s+)?between\b.{0,200}\b(?:borrower|lender|bank)\b", re.I | re.S),
    "DEFINITIONS": re.compile(r"\bdefinitions?\b(?:\s+and\s+interpretation)?|[\"“][A-Z][A-Za-z ]{1,40}[\"”]\s+"
                              r"(?:shall\s+)?means?\b", re.I),
    "FINANCIAL_COVENANTS": re.compile(r"\bfinancial\s+covenants?\b|debt\s+service\s+coverage|\bDSCR\b|interest\s+"
                                      r"coverage|\bTOL\s*/\s*TNW\b|tangible\s+net\s+worth", re.I),
    "COVENANT_CLAUSE": re.compile(r"\b(?:affirmative|negative|information)\s+(?:covenants?|undertakings?)\b|"
                                  r"\bcovenants?\s+and\s+undertakings\b", re.I),
    "SCHEDULE": re.compile(r"^\s*(?:schedule|annexure)\s+[IVX\d]+|repayment\s+schedule|schedule\s+of\s+repayment",
                           re.I | re.M),
    "AMENDMENT_CLAUSE": re.compile(r"\bshall\s+be\s+(?:amended|substituted|deleted|replaced|inserted)\b|"
                                   r"\bamendment\s+(?:agreement|letter)\b", re.I),
    "BALANCE_SHEET": re.compile(r"\bbalance\s+sheet\b|statement\s+of\s+financial\s+position", re.I),
    "PROFIT_LOSS": re.compile(r"\b(?:statement\s+of\s+)?profit\s+(?:and|&)\s+loss\b|\bincome\s+statement\b", re.I),
    "AUDITOR_REPORT": re.compile(r"\bindependent\s+auditor'?s?\s+report\b|\bauditor'?s?\s+report\b", re.I),
    "CERTIFICATE_BODY": re.compile(r"\b(?:we|i)\s+hereby\s+certify\b|\bthis\s+is\s+to\s+certify\b|"
                                   r"\bcompliance\s+certificate\b", re.I),
    "SIGNATURE": re.compile(r"\bfor\s+and\s+on\s+behalf\s+of\b|\bauthori[sz]ed\s+signator(?:y|ies)\b|"
                            r"\bsigned\s+(?:by|for)\b|\bsignature\s*:|\bpartner\b.{0,40}membership\s+no", re.I | re.S),
}
FOR_TYPE = {
    "LOAN_AGREEMENT": ("PARTIES", "DEFINITIONS", "FINANCIAL_COVENANTS", "COVENANT_CLAUSE", "SCHEDULE", "SIGNATURE"),
    "SANCTION_LETTER": ("PARTIES", "FINANCIAL_COVENANTS", "SCHEDULE", "SIGNATURE"),
    "AMENDMENT": ("PARTIES", "AMENDMENT_CLAUSE", "FINANCIAL_COVENANTS", "SIGNATURE"),
    "FINANCIAL_STATEMENT": ("BALANCE_SHEET", "PROFIT_LOSS", "AUDITOR_REPORT", "SIGNATURE"),
    "COMPLIANCE_CERTIFICATE": ("CERTIFICATE_BODY", "FINANCIAL_COVENANTS", "SIGNATURE"),
    "AMENDED_AND_RESTATED": ("PARTIES", "DEFINITIONS", "FINANCIAL_COVENANTS", "COVENANT_CLAUSE", "SCHEDULE",
                             "SIGNATURE"),
    "GUARANTEE": ("PARTIES", "FINANCIAL_COVENANTS", "COVENANT_CLAUSE", "SIGNATURE"),
    "WAIVER_CONSENT": ("PARTIES", "FINANCIAL_COVENANTS", "SIGNATURE"),
}
MAX_PAGES_PER_ROLE = 5


def _line(text: str, start: int, end: int) -> str:
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return re.sub(r"\s+", " ", text[a:b if b != -1 else len(text)]).strip()[:200]


def key_pages(model: dict, doc_type: str | None) -> list[dict]:
    """``[{role, page, pages, quote}]`` for the roles of ``doc_type`` found in the page model. The signature is looked
    for from the end of the document (it is normally on the last pages)."""
    pages = model.get("pages", [])
    out = []
    for role in FOR_TYPE.get(doc_type or "", ()):
        pattern = ROLES[role]
        ordered = list(reversed(pages)) if role == "SIGNATURE" else pages
        hits = []
        for page in ordered:
            text = page.get("text") or ""
            m = pattern.search(text)
            if m:
                hits.append((page["no"], _line(text, m.start(), m.end())))
                if len(hits) >= MAX_PAGES_PER_ROLE or role == "SIGNATURE":
                    break
        if hits:
            out.append({"role": role, "page": hits[0][0], "pages": sorted(no for no, _ in hits), "quote": hits[0][1]})
    return out
