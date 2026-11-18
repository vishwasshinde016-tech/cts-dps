"""amendment_diff pack rules: per covenant ADD / CHANGE / DELETE with the clause reference and effective date, and
the full covenant set as amended (the base version's result with the changes applied)."""

from __future__ import annotations

import re
from datetime import date

from dps.extraction.agreement import _best_metric, extract_terms
from dps.skills.common import issue, line_at, make_field
from dps.skills.covenants.rules import as_pages, term_value

PACK = "amendment_diff"
CLAUSE_OP = re.compile(r"\b(?:sub-?)?clause\s+(\d+(?:\.\d+)*(?:\s*\([a-z0-9]+\))?)[^\n.]{0,120}?\bshall\s+(?:stand\s+)?"
                       r"(be\s+deleted|be\s+substituted|be\s+replaced|be\s+amended|be\s+inserted|stand\s+deleted|"
                       r"be\s+added)", re.I)
NEW_CLAUSE = re.compile(r"\b(?:a\s+)?new\s+(?:sub-?)?clause\s+(\d+(?:\.\d+)*)\s+(?:shall\s+be\s+)?(?:inserted|added)",
                        re.I)
EFFECTIVE = re.compile(r"\b(?:with\s+effect\s+from|effective\s+(?:from|date|on)\s*:?)\s*(?:the\s+)?(\d{1,2})"
                       r"(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], start=1)}


def _effective(pages: list[dict]) -> str | None:
    for p in pages:
        m = EFFECTIVE.search(p.get("text") or "")
        if m and (mo := MONTHS.get(m.group(2).lower()[:3])):
            try:
                return date(int(m.group(3)), mo, int(m.group(1))).isoformat()
            except ValueError:
                continue
    return None


def _ops(pages: list[dict]) -> list[dict]:
    ops = []
    for p in pages:
        text = p.get("text") or ""
        for m in CLAUSE_OP.finditer(text):
            verb = m.group(2).lower()
            kind = "DELETE" if "delet" in verb else ("ADD" if "insert" in verb or "add" in verb else "CHANGE")
            window = text[m.start(): m.end() + 400]
            ops.append({"clause": re.sub(r"\s+", "", m.group(1)), "type": kind, "page": p["no"],
                        "quote": line_at(text, m.start(), m.end()), "metric": _best_metric(window)})
        for m in NEW_CLAUSE.finditer(text):
            window = text[m.start(): m.end() + 400]
            ops.append({"clause": m.group(1), "type": "ADD", "page": p["no"],
                        "quote": line_at(text, m.start(), m.end()), "metric": _best_metric(window)})
    return ops


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    """``ctx['base']`` is the base version's covenant fields (dps.result fields); changes are found against it."""
    base = {f["value"].get("metric"): f for f in ctx.get("base", []) if f["group"].endswith("COVENANT")}
    effective = _effective(pages)
    ops = _ops(pages)
    new_terms = [t for t in extract_terms(as_pages(pages)) if t.category in ("FINANCIAL", "NON_FINANCIAL")]
    out, touched = [], {}
    for t in new_terms:
        op = next((o for o in ops if o["metric"] == t.metric and o["type"] != "DELETE"), None)
        old = base.get(t.metric)
        value = term_value(t)
        if old is None:
            kind = "ADD"
        elif (old["value"].get("threshold"), old["value"].get("operator")) != (t.threshold, t.operator):
            kind = "CHANGE"
        else:
            continue  # restated unchanged: no change
        value["change"] = {"type": kind, "clause": op["clause"] if op else None, "effectiveDate": effective,
                           "appliedCovenantId": (old["value"].get("appliedCovenantId") if old else None),
                           "from": {"operator": old["value"].get("operator"),
                                    "threshold": old["value"].get("threshold")} if old else None}
        f = make_field(PACK, pages, old["group"] if old else "FINANCIAL_COVENANT", t.name or t.metric, value, t.page,
                       t.quote, confidence=0.85, key=t.metric)
        touched[t.metric] = touched.get(t.metric, 0) + 1
        out.append(f)
    for o in ops:
        if o["type"] != "DELETE" or not o["metric"] or o["metric"] not in base:
            continue
        old = base[o["metric"]]
        value = {**old["value"], "change": {"type": "DELETE", "clause": o["clause"], "effectiveDate": effective,
                                            "appliedCovenantId": old["value"].get("appliedCovenantId"),
                                            "from": None}}
        touched[o["metric"]] = touched.get(o["metric"], 0) + 1
        out.append(make_field(PACK, pages, old["group"], old["name"], value, o["page"], o["quote"], confidence=0.85,
                              key="del-" + o["metric"]))
    for f in out:
        if touched.get(f["value"].get("metric"), 0) > 1:
            f["issues"].append(issue("CHANGED_TWICE", "the amendment changes this covenant more than once"))
            f["status"] = "NEEDS_REVIEW"
    if base and ctx.get("baseSource") == "DPS_EXTRACTION":
        for f in out:  # the comparison used DPS's own earlier reading, not the approved covenants in the CTS DB
            f["issues"].append(issue("BASE_FROM_DPS", "compared with DPS's earlier extraction; send baseCovenants"))
    if not base:
        for f in out:
            f["issues"].append(issue("BASE_UNKNOWN", "the amended agreement's result was not found; check the base"))
            f["status"] = "NEEDS_REVIEW"
    return out


def amended_set(base: list[dict], changes: list[dict]) -> list[dict]:
    """The covenants as amended: base minus deletions, changed ones replaced, additions appended."""
    by_metric = {f["value"].get("metric"): dict(f) for f in base if f["group"].endswith("COVENANT")}
    for c in changes:
        metric = c["value"].get("metric")
        kind = c["value"]["change"]["type"]
        if kind == "DELETE":
            by_metric.pop(metric, None)
        else:
            by_metric[metric] = c
    return list(by_metric.values())


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    warnings = []
    base_clauses = set(ctx.get("baseClauses") or [])
    for f in fields:
        clause = (f["value"].get("change") or {}).get("clause")
        if base_clauses and clause and f["value"]["change"]["type"] != "ADD" and clause not in base_clauses:
            f["issues"].append(issue("CLAUSE_NOT_IN_BASE", f"clause {clause} is not in the amended agreement"))
            f["status"] = "NEEDS_REVIEW"
    if not fields:
        warnings.append(issue("NOTHING_FOUND", "no covenant changes found in the amendment"))
    return warnings
