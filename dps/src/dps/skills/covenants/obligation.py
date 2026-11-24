"""Obligation model v2 (full review WP6: F3, F4, G27): who a covenant binds, on whose figures, for which facility,
and when it applies.

Every covenant-like field gets, in its ``value``:

- ``scope``: ``{obligor, basis, facilityRefs, confidence}``. ``obligor`` is the party the clause binds as written
  (Borrower, Guarantor, Parent, Group ...); ``basis`` STANDALONE or CONSOLIDATED; ``confidence`` STATED (the quote
  says it), INFERRED (the Document Agent or the context says it) or UNKNOWN. A financial covenant whose company is
  unknown, or that binds several companies at once, goes to a person (``SCOPE_UNKNOWN``, ``SCOPE_SEVERAL``): Spring
  would otherwise test the wrong company's figures.
- ``conditions``: the "if / when / so long as" triggers (a springing covenant), each ``{text, kind, metric, operator,
  threshold}``. A condition always means review (``CONDITIONAL``): a springing covenant is not tested every period.
- ``baskets``: the permitted amounts of a negative covenant ("not exceeding USD 5,000,000 in aggregate").
- ``equityCure``: the agreement's equity cure right (once per document, on its financial covenants).

and in ``test``: ``firstTestDate`` and ``holiday`` (testing starts later). Read from the field's own quote and
qualifiers, then from the Document Agent's answer (``_ai`` hints put there by the vote); the rules never invent a
value the text does not hold. Fields a person already decided keep their status: only the new values are added.
"""

from __future__ import annotations

import re

from dps.extraction.schedule import DATE, parse_date
from dps.money import CURRENCY_AFTER, MONEY_MARKER, currency_after_amount, currency_before_amount, document_currencies
from dps.skills.common import issue

COVENANT_GROUPS = ("FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT")
BASIS_WORDS = {"CONSOLIDATED": "group figures (consolidated)", "STANDALONE": "company figures (standalone)"}
AI_KEYS = ("obligor", "facilityRefs", "conditions", "firstTestDate", "equityCure", "baskets")
OPEN = ("AUTO_ACCEPTED", "NEEDS_REVIEW")

PARTY = (r"Parent(?:\s+Guarantor)?|Holdings|Guarantors?|(?:Original\s+|Co-?)?Borrowers?|Company|Issuer|Obligors?|"
         r"Group|Restricted\s+Subsidiar(?:y|ies)|Material\s+Subsidiar(?:y|ies)|Promoters?|Sponsors?|Lessee|Purchaser")
OBLIGOR = re.compile(r"\b(?:the|each|any|no\s+member\s+of\s+the|each\s+member\s+of\s+the|every)\s+(" + PARTY + r")\b"
                     r"(?:\s+and\s+(?:the|each)\s+(" + PARTY + r"))?", re.I)
DUTY_VERB = re.compile(r"\b(?:shall|must|will|undertakes?|agrees?|covenants?|may\s+not|is\s+not\s+to)\b", re.I)
CONSOLIDATED = re.compile(r"\bconsolidated\b|\bof\s+the\s+Group\b|\bGroup\s+(?:basis|level)\b", re.I)
STANDALONE = re.compile(r"\bstand[\s-]?alone\b|\bunconsolidated\b|\bon\s+a\s+solo\s+basis\b|\bsolo\b", re.I)
FACILITY = re.compile(r"\b((?:Term\s+|Revolving\s+(?:Credit\s+)?|Working\s+Capital\s+|Capex\s+|Bridge\s+)?"
                      r"(?:Facility|Tranche|Loan)\s+[A-Z0-9]{1,3}|Revolving\s+(?:Credit\s+)?Facility|"
                      r"Working\s+Capital\s+Facilit(?:y|ies)|Term\s+(?:Loan\s+)?Facility|Capex\s+Facility)\b")
# "If Excess Availability is less than USD 10,000,000 on any day, ...": the trigger, up to the comma or the duty.
CONDITION = re.compile(
    r"\b(only\s+if|if\s+and\s+only\s+if|if|where|when(?:ever)?|in\s+case|(?:for\s+)?so\s+long\s+as|"
    r"at\s+any\s+time\s+(?:when|that|during\s+which)|during\s+any\s+period\s+(?:in\s+which|when|during\s+which)|"
    r"on\s+and\s+after\s+the\s+date\s+(?:on\s+which|when))\s+"
    r"((?:the\s+)?[A-Za-z][^;:]{4,220}?)"
    r"(?=,\s|;|\.\s|\.$|\bthen\b|\b(?:the|each|no|any)\s+(?:" + PARTY + r")\s+shall\b|$)",
    re.I)
NOT_A_CONDITION = re.compile(r"^(?:any(?:\s+\w+)?\s*$|any\s+(?:are|is|be|such|thereof)\b|applicable|required|"
                             r"necessary|so\s+requested|requested|possible|practicable|relevant|appropriate|"
                             r"not\s+already|available)\b", re.I)
SPRINGING = re.compile(r"\b(?:availability|utili[sz](?:ation|ed)|drawn|outstanding|usage|revolving|"
                       r"aggregate\s+(?:principal\s+)?amount\s+of\s+(?:the\s+)?loans)\b", re.I)
ALWAYS = re.compile(r"\b(?:outstanding|in\s+force|remains?\s+(?:in\s+effect|available)|continuing|in\s+effect)\b",
                    re.I)
RATING = re.compile(r"\brating\b|\binvestment\s+grade\b", re.I)
LESS = re.compile(r"\b(?:less|lower|fewer)\s+than\b|\bfalls?\s+below\b|\bbelow\b|\bunder\b|\bdrops?\s+below\b", re.I)
MORE = re.compile(r"\b(?:more|greater|higher)\s+than\b|\bexceeds?\b|\babove\b|\bin\s+excess\s+of\b", re.I)
CURRENCY = MONEY_MARKER
MONEY = re.compile(CURRENCY + r"\s*([\d,]+(?:\.\d+)?)\s*(million|mn|billion|bn|crores?|cr|lakhs?|thousand)?", re.I)
# "not exceeding 5,000,000 euros": the currency after the amount
MONEY_AFTER = re.compile(r"(?<![\d.,])([\d,]*\d(?:\.\d+)?)\s*(million|mn|billion|bn|crores?|cr|lakhs?|thousand)?"
                         + CURRENCY_AFTER, re.I)
PERCENT = re.compile(r"([\d.]+)\s*(?:%|per\s*cent|percent)", re.I)
METRIC_WORDS = re.compile(r"^(?:the\s+)?((?:[A-Z][\w&'-]*\s+){0,4}[A-Z][\w&'-]*)")
BASKET = re.compile(r"\b(?:not\s+exceeding|up\s+to|not\s+(?:to\s+)?exceed|which\s+does\s+not\s+exceed|of\s+no\s+more\s+"
                    r"than)\s+(?:in\s+(?:the\s+)?aggregate\s+)?(?:an\s+amount\s+(?:equal\s+to\s+)?(?:of\s+)?)?"
                    + "(?:" + MONEY.pattern + "|" + MONEY_AFTER.pattern + r"\S*(?:\s+[A-Za-z]+){0,2})", re.I)
FIRST_TEST = re.compile(r"\b(?:first\s+(?:test(?:ing)?\s+date|Test\s+Date|Relevant\s+Period|testing)|"
                        r"commencing\s+(?:with|from|on)|starting\s+(?:with|from|on)|beginning\s+(?:with|on))"
                        r"[^.;]{0,80}?(" + DATE.pattern + r")", re.I)
HOLIDAY = re.compile(r"\b(?:shall\s+not\s+be\s+tested|will\s+not\s+be\s+tested|shall\s+not\s+apply|will\s+not\s+apply|"
                     r"not\s+be\s+(?:applicable|tested)|testing\s+holiday)[^.;]{0,40}?\b(?:until|before|prior\s+to)\b"
                     r"[^.;]{0,30}?(" + DATE.pattern + r")", re.I)
EQUITY_CURE = re.compile(r"\b(?:equity\s+cure|cure\s+amount|new\s+equity|equity\s+contribution|cure\s+right)\b",
                         re.I)
CURE_LIMIT = re.compile(r"\b(?:not|no)\s+more\s+than\s+(one|two|three|four|five|\d)\s*(?:\(\d\)\s*)?"
                        r"(?:times|occasions?)([^.;]{0,80})", re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
SCALE = {"million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "crore": 1e7, "crores": 1e7, "cr": 1e7,
         "lakh": 1e5, "lakhs": 1e5, "thousand": 1e3}


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _party(name: str) -> str:
    """One spelling per party: 'Borrowers' and 'Original Borrower' are the Borrower; 'Parent Guarantor' stays."""
    name = _flat(name)
    low = name.lower()
    if low.endswith("borrowers") or low == "co-borrower" or low == "coborrower":
        return "Borrower"
    if low == "guarantors":
        return "Guarantor"
    if low == "obligors":
        return "Obligor"
    if low.startswith("original "):
        return name.split(" ", 1)[1].title()
    return name[:1].upper() + name[1:]


def obligors(quote: str | None) -> list[str]:
    """The parties the clause binds, as written: the subject of its duty verb first ("The Guarantor shall ..."),
    then 'no member of the Group may ...'. Several when the duty names several ('the Borrower and the Guarantor
    shall each ...')."""
    text = _flat(quote)
    if not text:
        return []
    verb = DUTY_VERB.search(text)
    hits = list(OBLIGOR.finditer(text))
    if not hits:
        return []
    before = [m for m in hits if verb and m.end() <= verb.start()]
    chosen = before[-1] if before else hits[0]
    out = [_party(chosen.group(1))]
    if chosen.group(2):
        out.append(_party(chosen.group(2)))
    seen, kept = set(), []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower())
            kept.append(p)
    return kept


def _basis(raw) -> str | None:
    text = str(raw or "").strip().upper()
    return text if text in ("STANDALONE", "CONSOLIDATED") else None


def basis_of(quote: str | None) -> str | None:
    text = _flat(quote)
    if STANDALONE.search(text):
        return "STANDALONE"
    if CONSOLIDATED.search(text):
        return "CONSOLIDATED"
    return None


def facilities(quote: str | None) -> list[str]:
    out = []
    for m in FACILITY.finditer(_flat(quote)):
        name = _flat(m.group(1))
        if name not in out:
            out.append(name)
    return out[:5]


def _money(m: re.Match, local: dict | None = None) -> tuple[float | None, str | None]:
    raw = m.group(0)
    currency = re.match(CURRENCY, raw, re.I)
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, None
    scale = (m.group(2) or "").lower()
    code = (currency_before_amount(currency.group(0), local) if currency
            else currency_after_amount(m.string[m.end():m.end() + 60], local))
    return number * SCALE.get(scale, 1), code


def _test(text: str) -> dict:
    """The metric, operator and threshold a condition tests, when it says them ('Excess Availability is less than
    USD 10,000,000')."""
    out: dict = {"metric": None, "operator": None, "threshold": None, "unit": None}
    text = re.sub(r"^(?:the\s+)?(?:" + PARTY + r")['’]s\s+", "", text, flags=re.I)  # "the Borrower's rating"
    head = METRIC_WORDS.match(text)
    if head:
        words = re.sub(r"\s+(?:is|are|was|falls?|exceeds?|remains?|drops?)$", "", head.group(1).strip())
        if words and words.lower() not in ("the", "any", "an", "a"):
            out["metric"] = re.sub(r"[^A-Z0-9]+", "_", words.upper()).strip("_") or None
    less, more = LESS.search(text), MORE.search(text)
    if less and (not more or less.start() < more.start()):
        out["operator"] = "<"
    elif more:
        out["operator"] = ">"
    m = MONEY.search(text)
    if m:
        out["threshold"], out["unit"] = _money(m)
    else:
        p = PERCENT.search(text)
        if p:
            try:
                out["threshold"], out["unit"] = float(p.group(1)), "PERCENT"
            except ValueError:
                pass
    return out


def conditions(texts: list[str]) -> list[dict]:
    """Each 'if / when / so long as' trigger in the covenant's quote and qualifiers, once."""
    out, seen = [], set()
    for text in texts:
        flat = _flat(text)
        for m in CONDITION.finditer(flat):
            body = m.group(2).strip(" ,")
            if NOT_A_CONDITION.match(body) or len(body.split()) < 3:
                continue
            if re.search(r"so\s+long\s+as", m.group(1), re.I) and ALWAYS.search(body):
                continue  # "so long as any amount is outstanding": the life of the loan, not a trigger
            if re.match(r"(?:as\s+)?if\b", flat[max(0, m.start() - 3):m.start() + 2].strip(), re.I) and \
                    flat[max(0, m.start() - 3):m.start()].lower().endswith("as "):
                continue  # "as if": a comparison, not a trigger
            words = _flat(m.group(0))
            key = words.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            kind = "SPRINGING" if SPRINGING.search(body) else "RATING" if RATING.search(body) else "CONDITIONAL"
            tested = _test(body)
            if kind == "RATING" and not tested["metric"]:
                tested["metric"] = "CREDIT_RATING"
            out.append({"text": words[:300], "kind": kind, **tested})
    return out[:4]


def baskets(quote: str | None, local: dict | None = None) -> list[dict]:
    out = []
    for m in BASKET.finditer(_flat(quote)):
        money = MONEY.search(m.group(0)) or MONEY_AFTER.search(m.group(0))
        amount, currency = _money(money, local) if money else (None, None)
        out.append({"text": _flat(m.group(0))[:200], "amount": amount, "currency": currency})
    return out[:5]


def first_test_date(quote: str | None) -> str | None:
    m = FIRST_TEST.search(_flat(quote))
    if not m:
        return None
    d = DATE.search(m.group(1))
    day = parse_date(d) if d else None
    return day.isoformat() if day else None


def holiday(quote: str | None) -> dict | None:
    m = HOLIDAY.search(_flat(quote))
    if not m:
        return None
    d = DATE.search(m.group(1))
    day = parse_date(d) if d else None
    return {"until": day.isoformat() if day else None, "text": _flat(m.group(0))[:200]} if day else None


def equity_cure(pages: list[dict] | None) -> dict | None:
    """The agreement's equity cure right: the first sentence that grants one, with its limit of uses."""
    for p in pages or []:
        text = _flat(p.get("text"))
        m = EQUITY_CURE.search(text)
        if not m:
            continue
        start = text.rfind(". ", 0, m.start())
        start = start + 2 if start != -1 else 0
        end = text.find(". ", m.end() + 20)  # past a heading ("Equity Cure. The Parent may ...")
        sentence = text[start:end + 1 if end != -1 else len(text)][:400]
        if re.search(r"\bno\s+equity\s+cure\b|\bnot\s+(?:be\s+)?(?:entitled|permitted)\s+to\s+cure\b", sentence, re.I):
            return {"allowed": False, "maxUses": None, "window": None, "page": p.get("no"), "text": sentence}
        limit = CURE_LIMIT.search(text[start:start + 1200])
        uses = None
        if limit:
            word = limit.group(1).lower()
            uses = WORDS.get(word) or (int(word) if word.isdigit() else None)
        return {"allowed": True, "maxUses": uses, "window": _flat(limit.group(2))[:120] if limit else None,
                "page": p.get("no"), "text": sentence}
    return None


def _hints(value: dict) -> dict:
    """What the Document Agent said about the v2 attributes: the vote's ``_ai`` hints on a rules field, or an
    agent-only field's own keys. All are taken out of the value; ``enrich`` rebuilds them from the text."""
    hints = dict(value.pop("_ai", None) or {})
    for k in AI_KEYS:
        v = value.pop(k, None)
        if v not in (None, "", [], {}):
            hints.setdefault(k, v)
    return hints


def clause_of(quote: str | None, threshold) -> str:
    """The sentence of the quote that sets the covenant: the one holding its threshold. A rules quote may run over
    the clause before it ('21.1 The Borrower shall ... 1.25. 21.2 The Guarantor shall ... 1.10'), and that clause's
    company must not become this one's."""
    from dps.extraction.verify import threshold_in
    from dps.skills.covenants.pages import sentences

    text = _flat(quote)
    parts = sentences(text)
    if len(parts) < 2 or not isinstance(threshold, int | float) or isinstance(threshold, bool):
        return text
    hits = [s for s in parts if threshold_in(s, float(threshold))]
    return hits[-1] if hits else text


def _flag(f: dict, code: str, message: str) -> None:
    if any(i.get("code") == code for i in f.get("issues") or []):
        return
    f.setdefault("issues", []).append(issue(code, message))
    if f.get("status") in OPEN:
        f["status"] = "NEEDS_REVIEW"


def _review_obligation(f: dict, stated: list[str], expected: str | None) -> None:
    found = f["value"]["conditions"]
    scope = f["value"]["scope"]
    basis, confidence, obligor = scope["basis"], scope["confidence"], scope["obligor"]
    if found:
        _flag(f, "CONDITIONAL", "applies only when: " + "; ".join(c["text"] for c in found)[:240])
    if f.get("group") == "FINANCIAL_COVENANT" and basis and expected and basis != expected:
        _flag(f, "ENTITY_DIFFERS", f"the agreement tests this on {BASIS_WORDS[basis]}; this borrower's "
                                   f"statements are read for {BASIS_WORDS[expected]}")
    if f.get("group") == "FINANCIAL_COVENANT" and confidence == "UNKNOWN":
        _flag(f, "SCOPE_UNKNOWN", "the clause does not say whose figures are tested; set the company")
    if len(stated) > 1:
        _flag(f, "SCOPE_SEVERAL", f"the clause binds {obligor}; if their figures are tested apart, split it")


def enrich(fields: list[dict], pages: list[dict] | None = None, ctx: dict | None = None, review: bool = True) -> None:
    """Adds scope, conditions, baskets, first test date, holiday and equity cure to every covenant-like field, and
    (``review``) sends the ones a person must decide to review. The proven agreement flow's stored terms keep the
    review state that flow gave them (``review=False``: values only). Safe to run twice."""
    ctx = ctx or {}
    cure = None
    cure_read = False
    local = document_currencies(" ".join(p.get("text") or "" for p in pages or []))  # "$" in an AUD agreement
    for f in fields:
        value = f.get("value")
        if not isinstance(value, dict):
            continue
        if f.get("group") not in COVENANT_GROUPS:
            value.pop("_ai", None)  # a waiver's or a term's own keys stay as they are
            continue
        hints = _hints(value)
        whole = _flat((f.get("source") or {}).get("quote"))  # dates may follow the clause ("...; not tested until")
        quote = clause_of(whole, value.get("threshold"))
        texts = [quote, *[q for q in value.get("qualifiers") or [] if isinstance(q, str)]]
        stated = obligors(quote)
        test = f.get("test") if isinstance(f.get("test"), dict) else {}
        basis = basis_of(quote) or _basis(test.get("basis")) or _basis(value.get("basis"))
        ai_obligor = hints.get("obligor")
        if stated:
            obligor, confidence = "; ".join(stated), "STATED"
        elif ai_obligor:
            obligor, confidence = _party(str(ai_obligor))[:80], "INFERRED"
        else:
            obligor, confidence = None, "UNKNOWN"
        refs = facilities(quote) or [str(r)[:60] for r in hints.get("facilityRefs") or [] if r][:5]
        if not refs and len(ctx.get("facilityIds") or []) == 1:
            refs = list(ctx["facilityIds"])  # the one facility this document is for (Spring's context)
        value["scope"] = {"obligor": obligor, "basis": basis, "facilityRefs": refs, "confidence": confidence}
        found = conditions(texts)
        for c in hints.get("conditions") or []:  # the agent's, when its text is in the quote (never invented)
            text = c.get("text") if isinstance(c, dict) else c
            if isinstance(text, str) and _flat(text).lower() in _flat(" ".join(texts)).lower() and not any(
                    _flat(text).lower() in x["text"].lower() or x["text"].lower() in _flat(text).lower()
                    for x in found):
                kind = (c.get("kind") if isinstance(c, dict) else None) or "CONDITIONAL"
                found.append({"text": _flat(text)[:300], "kind": kind if kind in ("SPRINGING", "RATING",
                                                                                   "CONDITIONAL") else "CONDITIONAL",
                              **_test(_flat(text))})
        value["conditions"] = found
        if f.get("group") == "NON_FINANCIAL_COVENANT" or not value.get("threshold"):
            value["baskets"] = baskets(quote, local)
        start = first_test_date(whole) or (hints.get("firstTestDate") if isinstance(hints.get("firstTestDate"), str)
                                           and DATE.search(whole) else None)
        pause = holiday(whole)
        if start or pause or f.get("test") is not None:
            test = dict(f.get("test") or {})
            test.setdefault("frequency", None)
            test.setdefault("basis", basis)
            if start and not test.get("firstTestDate"):
                test["firstTestDate"] = start
            else:
                test.setdefault("firstTestDate", None)
            if pause:
                test["holiday"] = pause
            f["test"] = test
        if f.get("group") == "FINANCIAL_COVENANT":
            if not cure_read:
                cure, cure_read = equity_cure(pages), True
            if cure is not None:
                value["equityCure"] = cure
        if not review or f.get("status") not in OPEN:
            continue  # a person already decided (or the proven flow did): the new values are shown, nothing else
        _review_obligation(f, stated, ctx.get("basis"))
