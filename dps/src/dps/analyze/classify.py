"""Document classification (Phase 3): rules first, a grounded model answer only when the rules are unsure.

Rules score every class from 0 to 1 from signal patterns on the first two pages (and a few whole-document
signals). When the top class scores >= 0.8 and leads the next by >= 0.3, the rules decide alone (no model call).
Otherwise the small model route reads the first two pages and must quote 2-3 lines that appear verbatim on the
pages it names; an ungrounded answer is invalid. Against the user's chosen type: a different type with
confidence >= 0.8 is TYPE_MISMATCH; confidence < 0.6 is UNCERTAIN_TYPE.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CLASSES = ("SANCTION_LETTER", "LOAN_AGREEMENT", "AMENDMENT", "FINANCIAL_STATEMENT", "COMPLIANCE_CERTIFICATE",
           "SECURITY_DOCUMENT", "OTHER_EVIDENCE", "WAIVER_CONSENT", "GUARANTEE", "AMENDED_AND_RESTATED", "TRUST_DEED")
# A chosen type the document may also read as without a mismatch (full review WP9): an amended and restated
# agreement is a loan agreement; a guarantee is a security document.
COMPATIBLE = {"LOAN_AGREEMENT": {"AMENDED_AND_RESTATED"}, "AMENDED_AND_RESTATED": {"LOAN_AGREEMENT"},
              "SECURITY_DOCUMENT": {"GUARANTEE"}, "GUARANTEE": {"SECURITY_DOCUMENT"}}
DECIDE_SCORE, DECIDE_LEAD = 0.8, 0.3
MISMATCH_CONFIDENCE, UNCERTAIN_CONFIDENCE = 0.8, 0.6
MAX_PROMPT_CHARS = 16_000  # about 4,000 tokens

# (pattern, weight). Weights add up per class and are capped at 1.
SIGNALS: dict[str, list[tuple[re.Pattern, float]]] = {
    "SANCTION_LETTER": [
        (re.compile(r"\bsanction\s+letter\b", re.I), 0.6),
        (re.compile(r"pleased\s+to\s+(inform|sanction|convey)", re.I), 0.4),
        (re.compile(r"terms\s+and\s+conditions\s+of\s+(the\s+)?sanction", re.I), 0.4),
        (re.compile(r"\bhas\s+been\s+sanctioned\b|\bsanctioned\s+(limit|amount)\b", re.I), 0.3),
        (re.compile(r"\bref(erence)?\.?\s*no\.?\s*[:\-]", re.I), 0.1),
        (re.compile(r"\bdear\s+(sir|madam|sirs)\b", re.I), 0.15),
    ],
    "LOAN_AGREEMENT": [
        (re.compile(r"\b(facilit(?:y|ies)|loan|term\s+loan|credit|working\s+capital|rupee\s+term\s+loan)\s+agreement\b",
                    re.I), 0.5),
        (re.compile(r"\bbetween\b.{0,400}\(.{0,40}\bborrower\b.{0,10}\).{0,600}\band\b.{0,400}"
                    r"\(.{0,40}\b(lender|bank|agent)s?\b", re.I | re.S), 0.4),
        (re.compile(r"\bevent\s+of\s+default\b", re.I), 0.15),
        (re.compile(r"\binterest\s+period\b", re.I), 0.1),
        (re.compile(r"\bthis\s+([\w-]+\s+){0,4}agreement\s+is\s+(made|entered)", re.I), 0.2),
        (re.compile(r"hereinafter\s+(referred\s+to|called)\s+as\s+(the\s+)?[\"“]?(borrower|lender)", re.I), 0.3),
        (re.compile(r"^\s*lender\s*:.*?^\s*borrower\s*:|^\s*borrower\s*:.*?^\s*lender\s*:", re.I | re.S | re.M), 0.3),
        (re.compile(r"\b(agrees?|agreed)\s+to\s+(make\s+available|lend|advance)\b", re.I), 0.2),
        # bulk run (22 Nov 2026): a bank's master facility agreement or master terms for its term loans
        (re.compile(r"\bmaster\s+(?:facility|loan|credit)\s+agreement\b|\bmaster\s+terms\s+and\s+conditions\b"
                    r"\s*\(?(?:applicable\s+(?:for|to)\s+)?(?:the\s+)?(?:term\s+loan|facilit(?:y|ies)|loans?)\b", re.I),
         0.6),
    ],
    "AMENDMENT": [
        (re.compile(r"\bamendment\s+(agreement|letter|no\.?)\b|\bsupplemental\s+(agreement|deed)\b", re.I), 0.6),
        (re.compile(r"\bamends?\s+(the\s+)?.{0,80}agreement\s+dated\b", re.I | re.S), 0.4),
        (re.compile(r"\bshall\s+be\s+(deleted|substituted)\s+(and\s+replaced\s+)?(with|by)\b", re.I), 0.3),
        (re.compile(r"\b(stand|shall\s+stand)\s+amended\b", re.I), 0.3),
        # US style: "FIRST AMENDMENT TO SECOND AMENDED AND RESTATED CREDIT AGREEMENT (this "Amendment")"
        (re.compile(r"\bamendment\s+(?:no\.?\s*\d+\s+)?to\s+(?:the\s+)?(?:[\w-]+\s+){0,6}agreement\b", re.I), 0.6),
        (re.compile(r"\(\s*this\s+[\"“]?amendment[\"”]?\s*\)", re.I), 0.3),
        # "AMENDMENT DEED No. 1" (Australia), "Deed of Amendment" (UK)
        (re.compile(r"\b(?:amendment|amending)\s+deed\b|\bdeed\s+of\s+amendment\b", re.I), 0.6),
        # a lender's letter: "the Loan Agreement is hereby amended as follows"
        (re.compile(r"\b(?:is|are)\s+(?:hereby\s+)?amended\s+(?:as\s+follows|to\s+read)\b", re.I), 0.3),
    ],
    "FINANCIAL_STATEMENT": [
        (re.compile(r"\bbalance\s+sheet\s+as\s+(at|on)\b", re.I), 0.5),
        (re.compile(r"\bstatement\s+of\s+profit\s+(and|&)\s+loss\b|\bprofit\s+and\s+loss\s+(account|statement)\b",
                    re.I), 0.4),
        (re.compile(r"\bcash\s+flow\s+statement\b", re.I), 0.4),
        (re.compile(r"\b(amounts?|figures)\s+in\s+(₹|rs\.?|inr|usd)?\s*(crores?|lakhs?|millions?|thousands?)\b"
                    r"|(₹|\brs\.?|\binr)\s*in\s+(crores?|lakhs?|millions?|thousands?)\b"
                    r"|\b(quarterly|monthly)\s+MIS\b|\bkey\s+figures\b", re.I), 0.2),
        (re.compile(r"\bbalance\s+sheet\b|\bschedule\s+III\b", re.I), 0.3),
        (re.compile(r"\bindependent\s+auditor'?s?'?\s+report\b", re.I), 0.3),
        (re.compile(r"\b(total\s+assets|total\s+equity\s+and\s+liabilities|revenue\s+from\s+operations)\b",
                    re.I), 0.3),
        (re.compile(r"\b(income\s+statement|statement\s+of\s+financial\s+position)\b", re.I), 0.4),
    ],
    "COMPLIANCE_CERTIFICATE": [
        (re.compile(r"\bcompliance\s+certificate\b", re.I), 0.6),
        (re.compile(r"\bwe\s+(hereby\s+)?certify\b|\bthis\s+is\s+to\s+certify\b", re.I), 0.3),
        (re.compile(r"\bcomplied\s+with\b|\bin\s+compliance\s+with\b", re.I), 0.2),
        (re.compile(r"\bUDIN\b", re.I), 0.3),
        (re.compile(r"\b(chartered\s+accountants?|membership\s+no)\b", re.I), 0.15),
    ],
    # Full review WP9 (F10): waiver and consent letters, guarantees with covenants, amended and restated agreements.
    "WAIVER_CONSENT": [
        (re.compile(r"\b(?:waiver(?:\s+and\s+consent)?|consent)\s+(?:letter|request|agreement)\b", re.I), 0.6),
        (re.compile(r"\b(?:hereby\s+)?(?:irrevocably\s+)?waives?\b|\bgrants?\s+(?:a|its|the)\s+waiver\b", re.I), 0.4),
        (re.compile(r"\bhereby\s+consents?\b|\bconsents?\s+to\s+the\b", re.I), 0.2),
        (re.compile(r"\b(?:non-?compliance\s+with|breach\s+of)\s+(?:the\s+)?(?:clause|covenant|financial)", re.I), 0.2),
        # US style: "WAIVER THIS WAIVER, dated as of ... (this "Waiver")", "LIMITED WAIVER", "Waiver and Amendment"
        (re.compile(r"\(\s*this\s+[\"“]?(?:limited\s+)?waiver[\"”]?\s*\)|\blimited\s+waiver\b|"
                    r"\bwaiver\s+and\s+(?:amendment|consent)\b", re.I), 0.6),
    ],
    "GUARANTEE": [
        (re.compile(r"\b(?:deed\s+of\s+|corporate\s+|parent\s+(?:company\s+)?)?guarantee\s+(?:agreement|and\s+"
                    r"indemnity|deed)\b|\bguaranty\s+agreement\b|\bdeed\s+of\s+guarantee\b", re.I), 0.6),
        (re.compile(r"\bguarantor\s+(?:hereby\s+)?(?:irrevocably\s+)?(?:and\s+unconditionally\s+)?guarantees?\b",
                    re.I), 0.4),
        (re.compile(r"\bguarantor\s+(?:shall|undertakes|covenants)\b", re.I), 0.2),
    ],
    "AMENDED_AND_RESTATED": [
        (re.compile(r"\bamended\s+and\s+restated\s+(?:[\w-]+\s+){0,4}agreement\b", re.I), 0.6),
        (re.compile(r"\bamends?\s+and\s+restates?\b|\bshall\s+be\s+amended\s+and\s+restated\b", re.I), 0.3),
    ],
    # Debenture and bond trust deeds (Sri Lanka, India): the issuer's covenants to the trustee for the holders
    "TRUST_DEED": [
        (re.compile(r"\b(?:debenture|bond|note|security)\s+trust\s+deed\b|\btrust\s+deed\s+(?:for|in\s+respect\s+of)\b"
                    r"[^\n]{0,120}\b(?:debentures?|bonds?|notes?)\b", re.I), 0.7),
        (re.compile(r"\b(?:debenture|bond|note)\s+trustees?\b|\btrustee\s+for\s+the\s+(?:debenture|bond|note)\s*holders\b",
                    re.I), 0.3),
        (re.compile(r"\b(?:debentures?|bonds?)\b[^\n]{0,80}\b(?:redeemable|secured|unsecured|listed|rated)\b",
                    re.I), 0.2),
        # "TRUST DEED AN INITIAL ISSUE OF UPTO FIFTY MILLION ... DEBENTURES" (Sri Lanka)
        (re.compile(r"\btrust\s+deed\b[\s\S]{0,300}?\bissue\s+of\b[\s\S]{0,200}?\bdebentures\b", re.I), 0.5),
        (re.compile(r"\b(?:initial\s+|public\s+)*issue\s+of\b[^\n]{0,160}\bdebentures\b", re.I), 0.3),
    ],
    "SECURITY_DOCUMENT": [
        (re.compile(r"\bdeed\s+of\s+(hypothecation|guarantee|pledge|mortgage)\b", re.I), 0.6),
        (re.compile(r"\bmemorandum\s+of\s+(entry|deposit\s+of\s+title)", re.I), 0.5),
        (re.compile(r"\b(hypothecat|pledg|guarantor)\w*\b", re.I), 0.2),
        (re.compile(r"\bschedule\s+of\s+(assets|properties|hypothecated)", re.I), 0.3),
    ],
    "OTHER_EVIDENCE": [
        (re.compile(r"\bstock\s+statement\b|\bstatement\s+of\s+stock\b", re.I), 0.6),
        (re.compile(r"\b(insurance\s+policy|policy\s+schedule|sum\s+insured)\b", re.I), 0.5),
        (re.compile(r"\bvaluation\s+report\b", re.I), 0.6),
        (re.compile(r"\b(credit\s+)?rating\s+(letter|rationale)\b", re.I), 0.6),
        (re.compile(r"\b(raw\s+materials?|finished\s+goods|work[\s-]+in[\s-]+progress|drawing\s+power|"
                    r"sundry\s+debtors)\b", re.I), 0.3),
    ],
}
EVIDENCE_KIND = {"stock": "STOCK_STATEMENT", "insur": "INSURANCE", "valuation": "VALUATION", "rating": "RATING"}
# Statement line items: a page that lists several of them is a statement or MIS pack, whatever its title.
LINE_ITEMS = re.compile(r"\b(revenue(\s+from\s+operations)?|total\s+revenue|ebitda|depreciation|finance\s+costs?|"
                        r"interest\s+expense|profit\s+(before|after)\s+tax|net\s+profit|net\s+worth|total\s+equity|"
                        r"(long|short)[\s-]+term\s+(debt|borrowings)|total\s+borrowings|current\s+(assets|liabilities)|"
                        r"cash\s+and\s+(bank\s+balances|cash\s+equivalents)|tax\s+expense|borrowings|"
                        r"trade\s+payables|equity\s+share\s+capital|net\s+cash\s+(generated|used)|"
                        r"total\s+current\s+(assets|liabilities)|property,\s+plant\s+and\s+equipment)\b", re.I)
SHEET_HINT = re.compile(r"^(bs|pl|p&l|cf|balance\s*sheet|profit|cash\s*flow)\b", re.I)


@dataclass
class Classification:
    type: str
    confidence: float
    method: str  # RULES | MODEL | VISION
    scores: dict[str, float]
    expected: str | None = None
    mismatch: bool = False
    uncertain: bool = False
    basis: str | None = None
    period_type: str | None = None
    evidence: list[dict] = field(default_factory=list)
    evidence_kind: str | None = None
    disputed: str | None = None  # the Document Agent's different answer

    def as_result(self) -> dict:
        return {"type": self.type, "expected": self.expected, "mismatch": self.mismatch, "basis": self.basis,
                "periodType": self.period_type, "method": self.method, "confidence": round(self.confidence, 3),
                "evidence": self.evidence, **({"aiSecondOpinion": self.disputed} if self.disputed else {})}


HEAD_SCAN, MIN_HEAD_CHARS = 8, 20
# An SEC filing's cover line, the only text on an exhibit's first page: "EX-10.20 3 exhibit1020sunseekercred.htm"
_FILING_LINE = re.compile(r"^\s*EX-[\d.]+\s+\d+\s+\S+\.htm\b[^\n]{0,60}$", re.I)


def _pages(model: dict, count: int = 2) -> list[tuple[int, str]]:
    """The first ``count`` pages that say something, among the first HEAD_SCAN: a blank page and an SEC filing's
    cover line come before the title in exhibits (bulk run, 22 Nov 2026: a credit agreement was UNKNOWN because its
    two first pages were those). A short title page counts. Without such pages, the first ``count`` pages."""
    pages = model.get("pages", [])

    def says_something(text: str) -> bool:
        return len(re.findall(r"\w", text)) >= MIN_HEAD_CHARS and not _FILING_LINE.match(text.strip())

    full = [(p["no"], p.get("text") or "") for p in pages[:HEAD_SCAN] if says_something(p.get("text") or "")]
    return full[:count] or [(p["no"], p.get("text") or "") for p in pages[:count]]


def number_density(text: str) -> float:
    tokens = text.split()
    if not tokens:
        return 0.0
    return sum(bool(re.fullmatch(r"\(?-?[\d,]+(\.\d+)?\)?%?", t)) for t in tokens) / len(tokens)


def _resolve_conflicting_types(head: list[tuple[int, str]], scores: dict[str, float]) -> None:
    # A page-1 title "... LOAN AGREEMENT" outweighs a mention of the sanction letter it follows (bulk run, 22 Nov
    # 2026), unless the document is an amendment, a restatement, a waiver or a trust deed of that agreement
    other = max(scores["AMENDMENT"], scores["AMENDED_AND_RESTATED"], scores["WAIVER_CONSENT"], scores["TRUST_DEED"])
    if head and other < 0.6 and re.search(r"(?-i:\b(?:LOAN|FACILITY|FACILITIES|CREDIT)\s+AGREEMENT\b)",
                                          head[0][1][:600]):
        scores["LOAN_AGREEMENT"] = min(1.0, scores["LOAN_AGREEMENT"] + 0.3)
        scores["SANCTION_LETTER"] = max(0.0, scores["SANCTION_LETTER"] - 0.3)
    if scores["AMENDMENT"] >= 0.6:  # an amendment quotes the agreement it amends: do not call it the agreement
        scores["LOAN_AGREEMENT"] = max(0.0, scores["LOAN_AGREEMENT"] - 0.3)
        # "First Amendment to the Amended and Restated Credit Agreement" is an amendment
        scores["AMENDED_AND_RESTATED"] = max(0.0, scores["AMENDED_AND_RESTATED"] - 0.5)
    elif scores["AMENDED_AND_RESTATED"] >= 0.6:  # a full agreement that replaces an earlier one (WP9)
        scores["AMENDED_AND_RESTATED"] = 1.0
        scores["LOAN_AGREEMENT"] = min(scores["LOAN_AGREEMENT"], 0.6)
    if scores["GUARANTEE"] >= 0.6 and scores["LOAN_AGREEMENT"] >= 0.5:  # titled as a facility or loan agreement
        # A facility agreement with a "Guarantee and Indemnity" clause (every LMA senior facilities agreement lists
        # one in its contents) is a loan agreement, not a guarantee (the architect's UK SFA, audit of 20 Nov 2026).
        scores["GUARANTEE"] = max(0.0, scores["GUARANTEE"] - 0.5)
    elif scores["GUARANTEE"] >= 0.6:  # a guarantee is a security document whose covenants bind the guarantor
        scores["SECURITY_DOCUMENT"] = max(0.0, scores["SECURITY_DOCUMENT"] - 0.4)
        scores["LOAN_AGREEMENT"] = max(0.0, scores["LOAN_AGREEMENT"] - 0.3)
    if scores["TRUST_DEED"] >= 0.7:  # a trust deed reads like a loan agreement and quotes the prospectus
        scores["LOAN_AGREEMENT"] = max(0.0, scores["LOAN_AGREEMENT"] - 0.4)
        scores["SECURITY_DOCUMENT"] = max(0.0, scores["SECURITY_DOCUMENT"] - 0.3)
    if scores["WAIVER_CONSENT"] >= 0.6:  # a waiver letter quotes the agreement and its covenants
        scores["LOAN_AGREEMENT"] = max(0.0, scores["LOAN_AGREEMENT"] - 0.3)
        scores["AMENDMENT"] = max(0.0, scores["AMENDMENT"] - 0.3)
        # ... and the compliance it waives ("the Borrower has not complied with Section 5.9"): not a certificate
        scores["COMPLIANCE_CERTIFICATE"] = max(0.0, scores["COMPLIANCE_CERTIFICATE"] - 0.4)


def rule_scores(model: dict) -> tuple[dict[str, float], list[dict]]:
    head = _pages(model)
    text = "\n".join(t for _, t in head)
    scores: dict[str, float] = {}
    evidence: dict[str, list[dict]] = {}
    for cls, signals in SIGNALS.items():
        total = 0.0
        for pattern, weight in signals:
            for no, page_text in head:
                m = pattern.search(page_text)
                if m:
                    total += weight
                    line = _line_of(page_text, m.start())
                    evidence.setdefault(cls, []).append({"page": no, "quote": line})
                    break
        scores[cls] = min(1.0, total)
    # Whole-document signals.
    whole = "\n".join(p.get("text") or "" for p in model.get("pages", []))
    if len(re.findall(r"\bshall\b", whole, re.I)) >= 30:
        scores["LOAN_AGREEMENT"] = min(1.0, scores["LOAN_AGREEMENT"] + 0.15)
    items = {m.group(0).lower() for m in LINE_ITEMS.finditer(text)}
    if len(items) >= 4:
        scores["FINANCIAL_STATEMENT"] = min(1.0, scores["FINANCIAL_STATEMENT"] + (0.7 if len(items) >= 6 else 0.5))
    if number_density(text) > 0.3:
        scores["FINANCIAL_STATEMENT"] = min(1.0, scores["FINANCIAL_STATEMENT"] + 0.3)
    if any(SHEET_HINT.match(s.get("name", "")) for s in model.get("sheets", [])):
        scores["FINANCIAL_STATEMENT"] = min(1.0, scores["FINANCIAL_STATEMENT"] + 0.4)
    _resolve_conflicting_types(head, scores)
    winner = max(scores, key=scores.get)
    return {k: round(v, 3) for k, v in scores.items()}, evidence.get(winner, [])[:3]


def _line_of(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start:end if end != -1 else len(text)].strip()[:300]


def sub_attributes(model: dict, cls: str) -> tuple[str | None, str | None, str | None]:
    text = "\n".join(p.get("text") or "" for p in model.get("pages", [])[:4])
    basis = period = kind = None
    if cls == "FINANCIAL_STATEMENT":
        basis = "CONSOLIDATED" if re.search(r"\bconsolidated\b", text, re.I) else (
            "STANDALONE" if re.search(r"\bstandalone\b", text, re.I) else None)
        if re.search(r"\bquarter\s+ended\b|\bthree\s+months\s+ended\b", text, re.I):
            period = "QUARTERLY"
        elif re.search(r"\bhalf[\s-]+year\s+ended\b|\bsix\s+months\s+ended\b", text, re.I):
            period = "HALF_YEARLY"
        elif re.search(r"\bmonth\s+ended\b", text, re.I):
            period = "MONTHLY"
        elif re.search(r"\byear\s+ended\b|\bas\s+at\s+31(st)?\s+march\b|\bannual\b", text, re.I):
            period = "ANNUAL"
    if cls == "OTHER_EVIDENCE":
        low = text.lower()
        kind = next((v for k, v in EVIDENCE_KIND.items() if k in low), None)
    return basis, period, kind


def decide_rules(scores: dict[str, float]) -> tuple[str, float] | None:
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, second = ranked[0], ranked[1]
    if top[1] >= DECIDE_SCORE and top[1] - second[1] >= DECIDE_LEAD:
        return top[0], top[1]
    return None


PROMPT = """You classify one bank lending document. {guard}
Classes: SANCTION_LETTER, LOAN_AGREEMENT, AMENDMENT, FINANCIAL_STATEMENT, COMPLIANCE_CERTIFICATE,
SECURITY_DOCUMENT, OTHER_EVIDENCE, WAIVER_CONSENT (a waiver or consent letter), GUARANTEE (a guarantee, deed of
guarantee or guaranty agreement), AMENDED_AND_RESTATED (a full agreement that amends and restates an earlier one),
TRUST_DEED (a debenture, bond or note trust deed), UNKNOWN.
Answer with JSON only: {{"type": "<class>", "basis": "STANDALONE"|"CONSOLIDATED"|null,
"periodType": "ANNUAL"|"HALF_YEARLY"|"QUARTERLY"|"MONTHLY"|null,
"evidence": [{{"page": <number>, "quote": "<a line copied exactly from that page>"}}]}} with 2 or 3 quotes.
Facts: {facts}
Headings: {headings}
{pages}"""

ANSWER_SCHEMA = {
    "type": "object",
    "required": ["type", "evidence"],
    "properties": {
        "type": {"enum": [*CLASSES, "UNKNOWN"]},
        "basis": {"enum": ["STANDALONE", "CONSOLIDATED", None]},
        "periodType": {"enum": ["ANNUAL", "HALF_YEARLY", "QUARTERLY", "MONTHLY", None]},
        "evidence": {"type": "array", "minItems": 1, "maxItems": 3,
                     "items": {"type": "object", "required": ["page", "quote"],
                               "properties": {"page": {"type": "integer"}, "quote": {"type": "string"}}}},
    },
}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def grounded(answer: dict, model: dict) -> str | None:
    pages = {p["no"]: _squash(p.get("text") or "") for p in model.get("pages", [])}
    for ev in answer.get("evidence", []):
        quote = _squash(ev.get("quote", ""))
        if len(quote) < 8 or quote not in pages.get(ev.get("page"), ""):
            return f"quote not found on page {ev.get('page')}: {ev.get('quote', '')[:80]!r}"
    return None


def model_classify(model: dict, gateway) -> dict | None:
    """The small route's grounded answer, or None when the gateway is unavailable or the answer is invalid.
    Image-only first pages go to the vision route instead (``vision_classify``)."""
    from dps.agent.gateway import GatewayError
    from dps.agent.guard import DATA_NOT_INSTRUCTIONS, model_safe_text

    head = [(no, model_safe_text(text)) for no, text in _pages(model)]
    if not any(t.strip() for _, t in head):
        return vision_classify(model, gateway)
    pages = "\n".join(f"--- page {no} ---\n{t}" for no, t in head)[:MAX_PROMPT_CHARS]
    headings = [model_safe_text(b["text"]) for p in model.get("pages", [])[:2] for b in p.get("blocks", [])
                if b.get("kind") == "HEADING"][:20]
    facts = {"format": model.get("format"), "pages": model.get("pageCount"),
             "sheets": [s.get("name") for s in model.get("sheets", [])][:10],
             "numberDensity": round(number_density("\n".join(t for _, t in head)), 2)}
    messages = [{"role": "user", "content": PROMPT.format(guard=DATA_NOT_INSTRUCTIONS, facts=facts,
                                                          headings=headings, pages=pages)}]
    safe_model = {"pages": [{"no": no, "text": text} for no, text in head]}
    try:
        return gateway.call_json("small", messages, ANSWER_SCHEMA, lambda a: grounded(a, safe_model))
    except GatewayError as ex:
        log.info("model classification unavailable: %s", ex.code)
        return None


def vision_classify(model: dict, gateway) -> dict | None:
    """Scanned first pages: the vision route reads their images (quotes cannot be checked against page text, so the
    answer is marked ``fromImage`` and the caller sends the document to a person)."""
    from dps.agent import vision
    from dps.agent.gateway import GatewayError
    from dps.agent.guard import DATA_NOT_INSTRUCTIONS

    images = vision.page_images.get()
    available = getattr(gateway, "available", None)
    if images is None or not callable(available) or not available("vision"):
        return None
    pngs = [(p["no"], images(p["no"])) for p in model.get("pages", [])[:2]]
    pngs = [(no, png) for no, png in pngs if png]
    if not pngs:
        return None
    allowed = {no for no, _ in pngs}
    text = PROMPT.format(guard=DATA_NOT_INSTRUCTIONS, facts={"format": model.get("format"),
                                                             "pages": model.get("pageCount")},
                         headings=[], pages="(the pages are the images below)")

    def check(answer) -> str | None:
        bad = [e for e in answer.get("evidence", []) if e.get("page") not in allowed]
        return f"page {bad[0].get('page')} was not one of the images" if bad else None

    try:
        answer = gateway.call_json("vision", vision.image_messages(text, pngs), ANSWER_SCHEMA, check)
    except GatewayError as ex:
        log.info("vision classification unavailable: %s", ex.code)
        return None
    return {**answer, "fromImage": True}


def classify(model: dict, expected: str | None = None, gateway=None) -> Classification:
    """Rules first, with a model opinion for uncertain or conflicting classifications."""
    scores, rule_evidence = rule_scores(model)
    decided = decide_rules(scores)
    confident = decided and decided[1] >= 0.95 and expected in (None, decided[0])
    skip = confident and getattr(gateway, "skip_confident", False)
    answer = model_classify(model, gateway) if gateway is not None and not skip else None
    if decided:
        cls, conf = decided
        result = Classification(cls, conf, "RULES", scores, evidence=rule_evidence)
        if answer and answer["type"] not in ("UNKNOWN", cls):
            result.disputed = answer["type"]
            result.evidence = [*rule_evidence[:2], *answer["evidence"][:2]]
        elif answer and answer["type"] == cls:
            result.method = "RULES+AI_CONFIRMED"
            result.confidence = max(conf, 0.95)
    elif answer and answer["type"] != "UNKNOWN":
        cls = answer["type"]
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        agrees = ranked[0][0] == cls and ranked[0][1] > 0
        conf = 0.9 if agrees else (0.75 if scores.get(cls, 0) >= 0.3 else 0.65)
        result = Classification(cls, conf, "VISION" if answer.get("fromImage") else "MODEL", scores,
                                evidence=answer["evidence"][:3])
        result.basis, result.period_type = answer.get("basis"), answer.get("periodType")
    else:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top, second = ranked[0], ranked[1]
        if top[1] >= 0.5 and top[1] - second[1] >= 0.2:
            result = Classification(top[0], round(min(0.79, top[1]), 3), "RULES", scores,
                                    evidence=rule_evidence)
        else:
            result = Classification("UNKNOWN", round(top[1] * 0.5, 3), "RULES", scores, evidence=rule_evidence)
    basis, period, kind = sub_attributes(model, result.type)
    result.basis = result.basis or basis
    result.period_type = result.period_type or period
    result.evidence_kind = kind
    result.expected = expected
    if expected:
        result.mismatch = (result.type not in (expected, "UNKNOWN", *COMPATIBLE.get(expected, ()))
                           and result.confidence >= MISMATCH_CONFIDENCE)
    result.uncertain = (result.confidence < UNCERTAIN_CONFIDENCE or result.disputed is not None
                        or result.method == "VISION")
    return result
