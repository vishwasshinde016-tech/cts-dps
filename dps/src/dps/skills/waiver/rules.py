"""waiver pack rules (full review WP9, F10): a waiver or consent letter, read against the approved covenants.

One WAIVER field per covenant the letter waives or consents to: the target covenant (matched to Spring's approved
``baseCovenants`` by clause, then by metric), the test dates or period waived, the conditions the waiver depends on,
when it expires and when the covenant applies again, and any fee. Every waiver is reviewed by a person: it changes
whether a breach is a breach.
"""

from __future__ import annotations

import re

from dps.extraction.agreement import _best_metric
from dps.extraction.schedule import DATE, parse_date
from dps.skills.common import issue, make_field
from dps.skills.covenants.obligation import MONEY, _money
from dps.skills.covenants.pages import sentences

PACK = "waiver"
WAIVE = re.compile(r"\b(?:hereby\s+)?(?:irrevocably\s+)?(?:(waive[sd]?|grants?\s+(?:a|its|the)\s+waiver|waiver\s+of)|"
                   r"(consents?\s+to|grants?\s+(?:its\s+)?consent)|"
                   r"(agrees?\s+not\s+to\s+(?:test|apply|declare|treat)|shall\s+not\s+(?:be\s+)?(?:tested|test|treat)))\b",
                   re.I)
# An extension of time (CTS integration, 23 Nov 2026: the bank app records an extension with its new date): "the
# Lender extends the date for delivery of the audited accounts to 31 December 2026", "grants an extension of time
# until ...". The new date is extendedTo.
EXTEND = re.compile(r"\b(?:extends?|extended|grants?\s+(?:an?\s+|its\s+)?extension|extension\s+of\s+(?:time|the\s+"
                    r"(?:date|deadline|period|time)))\b", re.I)
EXTENDED_TO = re.compile(r"\b(?:to|until|till|up\s*to|on\s+or\s+before|by)\s+(?:and\s+including\s+)?(?:the\s+)?(?:"
                         + DATE.pattern + r")", re.I)
# only an extension of a date or a time limit, never a facility extended to the borrower
EXT_OBJECT = re.compile(r"\b(?:date|deadline|time|period|timeline|due)\b", re.I)
KINDS = ("WAIVER", "CONSENT", "NOT_TESTED", "EXTENSION")
CLAUSE = re.compile(r"\b(?:clause|section|article|paragraph)\s+(\d+(?:\.\d+)*(?:\s*\([a-z0-9]{1,4}\))*)", re.I)
PERIOD_FROM_TO = re.compile(r"\bfrom\s+(?:and\s+including\s+)?(?P<a>" + DATE.pattern.replace("?P<", "?P<a_") + r")"
                            r"\s+(?:to|until|till|up\s+to)\s+(?:and\s+including\s+)?(?P<b>"
                            + DATE.pattern.replace("?P<", "?P<b_") + r")", re.I)
TEST_DATE = re.compile(r"\b(?:ending|ended|falling\s+on|as\s+(?:at|of|on)|test(?:ing)?\s+date|on)\s+"
                       r"(?:(?:on\s+)?or\s+about\s+|on\s+or\s+about\s+)?(?:the\s+)?(?:"
                       + DATE.pattern + r")", re.I)
CONDITION = re.compile(r"\b(?:subject\s+to|provided\s+(?:that|always)|on\s+(?:the\s+)?condition\s+that|"
                       r"conditional\s+(?:up)?on|in\s+consideration\s+of)\b[^.;]{5,300}", re.I)
EXPIRY = re.compile(r"\b(?:expire[sd]?|shall\s+cease|lapse[sd]?|valid\s+(?:until|till|up\s+to)|remain\s+in\s+(?:force|"
                    r"effect)\s+(?:until|till)|no\s+later\s+than)\b[^.;]{0,50}?(" + DATE.pattern + r")", re.I)
REINSTATE = re.compile(r"\b(?:shall\s+(?:again|thereafter|continue\s+to)\s+(?:apply|be\s+tested)|(?:be\s+)?reinstated|"
                       r"(?:from|with\s+effect\s+from)\s+the\s+(?:next|following|subsequent)\s+test(?:ing)?\s+date|"
                       r"shall\s+be\s+tested\s+(?:again|as\s+usual))\b[^.;]{0,160}", re.I)
FEE = re.compile(r"\b(?:waiver|consent|amendment)\s+fee\b[^.;]{0,120}", re.I)
NOT_WAIVED = re.compile(r"\b(?:does\s+not|shall\s+not|will\s+not)\s+(?:constitute\s+)?(?:a\s+)?waive|"
                        r"\bno\s+waiver\b|\bwithout\s+(?:any\s+)?waiver\b|\bnot\s+be\s+construed\s+as\s+a\s+waiver\b",
                        re.I)
LIST_INTRO = re.compile(r"\bhereby\s+waive\b[^.;]{0,160}?(?=\([ivx]+\)\s+breach\b)", re.I)
LIST_ITEM = re.compile(r"\([ivx]+\)\s+breach\b", re.I)
LIST_END = re.compile(r"\bprovided\s+(?:that\s+)?such\s+waivers?\b|\b\d+\.\s+Representations and Warranties\b",
                      re.I)
TERMINATION = re.compile(r"\bWaiver Termination Event[”\"']?\s+means\s+(?:the\s+)?earliest\b.*?"
                         r"(?=\s+\d+\.\s+Representations\b|$)", re.I)
RESTORED_AT_EXPIRY = re.compile(r"\bupon such expiration\b.*?\bas if such waivers? had not been given\b", re.I)


def _iso(m: re.Match | None) -> str | None:
    if m is None:
        return None
    d = DATE.search(m.group(0))
    day = parse_date(d) if d else None
    return day.isoformat() if day else None


def periods(text: str) -> list[dict]:
    """The waived test dates ('for the quarter ending 31 March 2026') and ranges ('from 1 April 2026 to 30 September
    2026'), in order, once each."""
    out: list[dict] = []
    for m in PERIOD_FROM_TO.finditer(text):
        a, b = DATE.search(m.group("a")), DATE.search(m.group("b"))
        da, db = (parse_date(a) if a else None), (parse_date(b) if b else None)
        if da and db:
            out.append({"from": da.isoformat(), "to": db.isoformat(), "testDate": None})
    ranged = " ".join(m.group(0) for m in PERIOD_FROM_TO.finditer(text))
    for m in TEST_DATE.finditer(text):
        day = _iso(m)
        if day and day not in ranged and not any(p.get("testDate") == day for p in out):
            if not any(p["from"] and p["from"] <= day <= p["to"] for p in out if p.get("from")):
                out.append({"from": None, "to": None, "testDate": day})
    return out[:12]


def _base_target(base: list[dict], metric: str | None, clause: str | None) -> dict | None:
    from dps.skills.amendment_diff.rules import _targets

    hits = _targets(base, metric, clause)
    return hits[0] if len(hits) == 1 else None


def _fee(text: str) -> dict | None:
    m = FEE.search(text)
    if not m:
        return None
    money = MONEY.search(m.group(0))
    if not money:
        return {"amount": None, "currency": None, "text": " ".join(m.group(0).split())[:200]}
    amount, currency = _money(money)
    return {"amount": amount, "currency": currency, "text": " ".join(m.group(0).split())[:200]}


def _operative_list(whole: str, page_text: list[tuple[int, str]]) -> list[tuple[int, str, str, str]]:
    """One target per breach in a clearly identified operative list, even across a page break."""
    intro = LIST_INTRO.search(whole)
    end = LIST_END.search(whole, intro.end()) if intro else None
    if end is None:
        return []
    markers = list(LIST_ITEM.finditer(whole, intro.end(), end.start()))
    if len(markers) < 2:
        return []
    ranges, offset = [], 0
    for no, text in page_text:
        ranges.append((no, offset, offset + len(text)))
        offset += len(text) + 1
    items = []
    for index, mark in enumerate(markers):
        limit = markers[index + 1].start() if index + 1 < len(markers) else end.start()
        no, _, page_end = next(r for r in ranges if r[1] <= mark.start() < r[2])
        text = whole[mark.start():limit].strip(" ,")
        quote = whole[mark.start():min(page_end, limit)].strip(" ,")
        items.append((no, "WAIVER", text, quote))
    return items


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    base = [f for f in ctx.get("base") or [] if str(f.get("group", "")).endswith("COVENANT")]
    page_text = [(int(p["no"]), " ".join((p.get("text") or "").split())) for p in pages]
    whole = " ".join(text for _, text in page_text)
    doc_conditions = [" ".join(m.group(0).split())[:300] for m in CONDITION.finditer(whole)][:6]
    expiry = _iso(EXPIRY.search(whole))
    reinstate = REINSTATE.search(whole) or RESTORED_AT_EXPIRY.search(whole)
    explicit = _operative_list(whole, page_text)
    termination = TERMINATION.search(whole) if explicit else None
    if termination:
        expiry = _iso(termination) or expiry
        doc_conditions.append(termination.group(0)[:400])
    fee = _fee(whole)
    candidates = list(explicit)
    for p in pages:
        listed = sentences(p.get("text") or "")
        for i, s in enumerate(listed):
            m = WAIVE.search(s)
            ext = EXTEND.search(s) if EXT_OBJECT.search(s) else None
            if ext and (m is None or ext.start() < m.start()):
                kind = "EXTENSION"
            elif m and not NOT_WAIVED.search(s):
                kind = "WAIVER" if m.group(1) else "CONSENT" if m.group(2) else "NOT_TESTED"
            else:
                continue
            following = listed[i + 1] if i + 1 < len(listed) else ""
            text = s + (" " + following if CONDITION.match(following.strip()) else "")
            if not explicit or kind != "WAIVER":
                candidates.append((int(p["no"]), kind, text, s))
    out, seen = [], set()
    for i, (page, kind, text, quote) in enumerate(candidates):
        clause_m = CLAUSE.search(text)
        clause = re.sub(r"\s+", "", clause_m.group(1)) if clause_m else None
        metric = _best_metric(text)
        target = _base_target(base, metric, clause)
        name = (target or {}).get("name") or metric or (f"Clause {clause}" if clause else "Waiver")
        key = (target or {}).get("id") or f"{metric}|{clause}"
        found = periods(text)
        if key in seen and not found:
            continue
        seen.add(key)
        conditions = [" ".join(c.group(0).split())[:300] for c in CONDITION.finditer(text)] or doc_conditions
        if termination and termination.group(0)[:400] not in conditions:
            conditions = [*conditions, termination.group(0)[:400]]
        extension = EXTEND.search(text) if kind == "EXTENSION" else None
        extended_to = _iso(EXTENDED_TO.search(text, extension.start())) if extension else None
        value = {"kind": kind,
                 "target": {"metric": (target or {}).get("value", {}).get("metric") or metric, "clause": clause,
                            "appliedCovenantId": (target or {}).get("value", {}).get("appliedCovenantId"),
                            "name": (target or {}).get("name")},
                 "periods": found, "conditions": conditions, "expiresOn": expiry,
                 "reinstatement": " ".join(reinstate.group(0).split())[:300] if reinstate else None,
                 "fee": fee, "metric": metric or "OTHER",
                 **({"extendedTo": extended_to} if kind == "EXTENSION" else {})}
        f = make_field(PACK, pages, "WAIVER", name, value, page, quote, "RULES", 0.7, key=f"{key}-{page}-{i}")
        if target is None:
            f["issues"].append(issue("WAIVER_TARGET_UNKNOWN", "the waived covenant is not one of the approved "
                                                              "covenants sent; choose it"))
        if kind == "EXTENSION" and extended_to is None:
            f["issues"].append(issue("WAIVER_PERIOD_UNKNOWN", "no new date found for the extension; set it"))
        elif kind != "EXTENSION" and not found:
            f["issues"].append(issue("WAIVER_PERIOD_UNKNOWN", "no test date or period found for the waiver; "
                                                              "set it"))
        f["status"] = "NEEDS_REVIEW"  # a waiver always goes to a person
        out.append(f)
    return out


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    warnings = []
    if not ctx.get("base"):
        warnings.append(issue("BASE_MISSING", "no approved covenants were sent (context.baseCovenants), so the "
                                              "waived covenants could not be matched; a person matches them"))
    if not [f for f in fields if f.get("group") == "WAIVER"]:
        warnings.append(issue("NOTHING_FOUND", "no waiver or consent found in the letter"))
    return warnings
