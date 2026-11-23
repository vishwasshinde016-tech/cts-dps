"""amendment_diff pack rules: per covenant ADD / CHANGE / DELETE with the clause reference and effective date, and
the full covenant set as amended (the base version's result with the changes applied)."""

from __future__ import annotations

import re
from datetime import date

from dps.extraction.agreement import _best_metric, extract_terms
from dps.skills.common import issue, line_at, make_field
from dps.skills.covenants.rules import as_pages, term_field

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
    unique = {}
    for operation in ops:
        unique.setdefault((operation["page"], operation["clause"], operation["type"]), operation)
    return list(unique.values())


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    """Find proposed changes against the base version's individual covenant records."""
    base = [f for f in ctx.get("base", []) if f["group"].endswith("COVENANT")]
    effective = _effective(pages)
    ops = _ops(pages)
    new_terms = [t for t in extract_terms(as_pages(pages)) if t.category in ("FINANCIAL", "NON_FINANCIAL")]
    out, touched, handled = [], {}, set()
    for t in new_terms:
        candidates = [o for o in ops if o["metric"] == t.metric and o["type"] != "DELETE" and o["page"] == t.page]
        term_clause = t.quote or ""
        matching = [o for o in candidates if re.search(r"\b" + re.escape(o["clause"]) + r"(?![.\d])", term_clause)]
        op = (matching or candidates)[0] if len(matching or candidates) == 1 else None
        possible = _targets(base, t.metric, op["clause"] if op else None)
        old = possible[0] if len(possible) == 1 and not (op and op["type"] == "ADD") else None
        f = term_field(PACK, pages, t, key=f"{t.metric}-{len(out)}")
        value = f["value"]
        if op:
            handled.add((op["page"], op["clause"], op["type"]))
        if old is None:
            kind = "CHANGE" if (op and op["type"] == "CHANGE") or (possible and not op) else "ADD"
        elif (op or any(value.get(k) is not None and value[k] != old["value"].get(k)
                        for k in ("threshold", "operator", "schedule", "cure", "expression"))
              or any((old.get("test") or {}).get(k) is not None and (f.get("test") or {}).get(k) is not None
                     and old["test"][k] != f["test"][k] for k in ("frequency", "basis"))):
            kind = "CHANGE"
        else:
            continue  # restated unchanged: no change
        value["change"] = {"type": kind, "clause": op["clause"] if op else None, "effectiveDate": effective,
                           "appliedCovenantId": (old["value"].get("appliedCovenantId") if old else None),
                           "from": {"operator": old["value"].get("operator"),
                                    "threshold": old["value"].get("threshold")} if old else None}
        if old:
            f["group"] = old["group"]
        if kind == "CHANGE" and old is None:
            _hold(f, "AMBIGUOUS_AMENDMENT_TARGET", "The change has no unique approved covenant target.")
        target_key = _change_key(value)
        touched[target_key] = touched.get(target_key, 0) + 1
        out.append(f)
    for o in ops:
        if (o["page"], o["clause"], o["type"]) in handled:
            continue
        possible = _targets(base, o["metric"], o["clause"])
        old = possible[0] if len(possible) == 1 else None
        metric = o["metric"] or (old or {}).get("value", {}).get("metric") or "OTHER"
        value = dict(old["value"]) if old else {
            "metric": metric, "operator": None, "threshold": None, "unit": None, "expression": None,
        }
        value["change"] = {"type": o["type"], "clause": o["clause"], "effectiveDate": effective,
                           "appliedCovenantId": value.get("appliedCovenantId"), "from": None}
        f = make_field(PACK, pages, old["group"] if old else "POSSIBLE_COVENANT",
                       old["name"] if old else f"Clause {o['clause']}", value, o["page"], o["quote"],
                       confidence=0.85, key=f"{o['type']}-{o['page']}-{o['clause']}")
        if old is None or o["type"] != "DELETE":
            _hold(f, "UNRESOLVED_AMENDMENT", "Confirm the target and revised terms against the approved agreement.")
        target_key = _change_key(value)
        touched[target_key] = touched.get(target_key, 0) + 1
        out.append(f)
    for f in out:
        if touched.get(_change_key(f["value"]), 0) > 1:
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


def _targets(base: list[dict], metric: str | None, clause: str | None) -> list[dict]:
    matching = [f for f in base if clause and re.sub(r"\s+", "", f["value"].get("clause") or "") == clause]
    if len(matching) == 1:
        return matching
    eligible = matching or [f for f in base if not clause or not f["value"].get("clause")]
    return [f for f in eligible if metric and f["value"].get("metric") == metric]


def _change_key(value: dict) -> str:
    change = value.get("change") or {}
    return change.get("appliedCovenantId") or change.get("clause") or value.get("metric")


def _hold(field: dict, code: str, message: str) -> None:
    field["issues"].append(issue(code, message))
    field["status"] = "NEEDS_REVIEW"


def amended_set(base: list[dict], changes: list[dict]) -> list[dict]:
    """The covenants as amended: base minus deletions, changed ones replaced, additions appended."""
    result = [dict(f) for f in base if f["group"].endswith("COVENANT")]
    for c in changes:
        if any(i["code"] in ("AMBIGUOUS_AMENDMENT_TARGET", "UNRESOLVED_AMENDMENT", "CHANGED_TWICE")
               for i in c.get("issues", [])):
            continue
        value = c.get("value") or {}
        change = value.get("change")
        if not isinstance(change, dict) or not change.get("type"):
            # Not a change: with the AI on, an amendment's result also holds the agent's own findings and the
            # schedule terms (bulk run, 22 Nov 2026: every amendment read with AI ended DEAD on KeyError 'change').
            continue
        if change["type"] == "ADD":
            result.append(c)
            continue
        target = change.get("appliedCovenantId")
        if target:
            positions = [i for i, f in enumerate(result)
                         if target in (f["value"].get("appliedCovenantId"),
                                       (f["value"].get("change") or {}).get("appliedCovenantId"))]
        else:
            positions = [i for i, f in enumerate(result) if f["value"].get("metric") == value.get("metric")]
        if len(positions) == 1:
            if change["type"] == "DELETE":
                result.pop(positions[0])
            else:
                result[positions[0]] = c
    return result


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
