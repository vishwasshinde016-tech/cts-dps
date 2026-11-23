"""covenants pack rules: today's agreement extractor (agreement.py + safety net), as dps.result fields."""

from __future__ import annotations

import re

from dps.extraction.agreement import Term, extract_terms
from dps.parse.engine import Page
from dps.skills.common import issue, make_field

GROUP = {"FINANCIAL": "FINANCIAL_COVENANT", "NON_FINANCIAL": "NON_FINANCIAL_COVENANT", "INFO": "INFO_COVENANT",
         "CANDIDATE": "POSSIBLE_COVENANT"}


def as_pages(pages: list[dict]) -> list[Page]:
    return [Page(p["no"], p.get("text") or "", p.get("sourceMethod") or "TEXT", float(p.get("quality", 1.0)))
            for p in pages]


def term_value(t: Term) -> dict:
    schedule = None
    if t.levels:
        schedule = [{"from": lv.get("from"), "to": lv.get("to"), "threshold": lv.get("threshold")} for lv in t.levels]
    return {"metric": t.metric, "operator": t.operator, "threshold": t.threshold, "unit": t.unit,
            "expression": None, "schedule": schedule, **({"currency": t.currency} if t.currency else {})}


def term_field(pack: str, pages: list[dict], t: Term, key: str = "") -> dict:
    issues = []
    for text in t.issues:
        code, _, message = str(text).partition(":")
        issues.append(issue(code.strip() or "ISSUE", message.strip() or None))
    f = make_field(pack, pages, GROUP.get(t.category, "INFO_COVENANT"), t.name or t.metric, term_value(t), t.page,
                   t.quote, t.method, t.confidence or 0.8, issues,
                   test={"frequency": t.frequency, "basis": t.basis, "firstTestDate": None}, key=key or t.metric)
    if t.cure:
        f["value"]["cure"] = t.cure
    if issues or t.category == "CANDIDATE":
        f["status"] = "NEEDS_REVIEW"
    return f


# Which way a covenant usually points (kept from the old agreement flow's validation, full review WP7): a
# coverage ratio read as a maximum, or a leverage ratio read as a minimum, is usually a misread.
FLOORS = {"DSCR", "ICR", "EBITDA_TO_EMI", "CURRENT_RATIO", "QUICK_RATIO", "FIXED_CHARGE_COVERAGE_RATIO",
          "MIN_TANGIBLE_NET_WORTH", "MIN_NET_WORTH", "MINIMUM_LIQUIDITY", "MIN_EBITDA"}
CEILINGS = {"DEBT_TO_EBITDA", "NET_LEVERAGE_RATIO", "DEBT_TO_NET_WORTH", "DEBT_TO_EQUITY", "LOAN_TO_VALUE"}


def direction_issue(metric: str | None, operator: str | None) -> str | None:
    """OPERATOR_UNEXPECTED, when the operator points the wrong way for the covenant's family."""
    family = str(metric or "").split(":", 1)[0]
    if family in FLOORS and operator in ("<", "<="):
        return "usually a minimum (>=)"
    if family in CEILINGS and operator in (">", ">="):
        return "usually a maximum (<=)"
    return None


# "If the DSCR is less than 1.20, the Borrower shall cure ...": the sentence states the breach, so the covenant is the
# opposite (DSCR >= 1.20). CTS review, 23 Nov 2026: the bare "less than" rule read it as DSCR < 1.20.
BREACH_WORDING = re.compile(r"^\s*(?:\(?[a-z0-9]{1,4}[.)]\s*)?(?:if|in\s+the\s+event\s+(?:that\s+)?|where|should|"
                            r"in\s+case)\b", re.I)
OPPOSITE = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def breach_to_covenant(f: dict) -> None:
    """A covenant read from breach wording gets the opposite operator, and a person checks it."""
    v = f.get("value") or {}
    quote = (f.get("source") or {}).get("quote") or ""
    op = v.get("operator")
    if op in OPPOSITE and BREACH_WORDING.search(quote) and not any(
            i.get("code") == "BREACH_WORDING" for i in f.get("issues") or []):
        v["operator"] = OPPOSITE[op]
        f.setdefault("issues", []).append(issue(
            "BREACH_WORDING", f"the clause states when it is breached ({op}); the covenant is {OPPOSITE[op]}"))
        f["status"] = "NEEDS_REVIEW"


def checks_for(fields: list[dict]) -> list[dict]:
    warnings = []
    for f in fields:
        v = f["value"]
        if f.get("group") == "FINANCIAL_COVENANT":
            breach_to_covenant(f)
        unusual = direction_issue(v.get("metric"), v.get("operator"))
        if unusual and not any(i.get("code") == "OPERATOR_UNEXPECTED" for i in f.get("issues") or []):
            f["issues"].append(issue("OPERATOR_UNEXPECTED", unusual))
            f["status"] = "NEEDS_REVIEW"
        schedule = v.get("schedule") or []
        starts = [s.get("from") for s in schedule if s.get("from")]
        if starts != sorted(starts):
            f["issues"].append(issue("STEP_DOWN_ORDER", "step-down dates are not in order"))
            f["status"] = "NEEDS_REVIEW"
        quote = (f["source"]["quote"] or "").lower()
        op = v.get("operator")
        if op in (">=", ">") and any(w in quote for w in ("not exceed", "not more than", "maximum", "at most")):
            f["issues"].append(issue("DIRECTION_MISMATCH", f"operator {op} but the clause sets a ceiling"))
            f["status"] = "NEEDS_REVIEW"
        if op in ("<=", "<") and any(w in quote for w in ("not less than", "at least", "minimum", "not below")):
            f["issues"].append(issue("DIRECTION_MISMATCH", f"operator {op} but the clause sets a floor"))
            f["status"] = "NEEDS_REVIEW"
    return warnings


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    """The agreement extractor, plus the safety net: covenant-shaped sentences no rule explained go to a person as
    POSSIBLE_COVENANT (so nothing is silently missed without an LLM)."""
    from dps.extraction.safety_net import possible_covenants

    read = as_pages(pages)
    terms = extract_terms(read)
    candidates, _ = possible_covenants(read, terms)
    fields = [term_field("covenants", pages, t, key=f"{t.metric}-{i}") for i, t in enumerate(terms + candidates)]
    return fields + step_tables(pages, fields)


CEILING = re.compile(r"not\s+(?:to\s+)?exceed|not\s+be\s+(?:more|greater|higher)|no\s+(?:more|greater)\s+than|"
                     r"\bmaximum\b|or\s+(?:less|lower)\b|\bat\s+most\b", re.I)
FLOOR = re.compile(r"not\s+(?:be\s+)?(?:less|lower)|\bat\s+least\b|\bminimum\b|or\s+(?:more|higher|greater)\b|"
                   r"no\s+less\s+than", re.I)


def step_tables(pages: list[dict], fields: list[dict]) -> list[dict]:
    """A covenant whose levels are a table ("shall not exceed the ratio set out opposite that date:" then dates and
    ratios) is one field with its schedule (full review F2: such tables gave no field and no review item). Always
    for a person: the table's reading of dates and levels is checked against the page."""
    from dps.extraction.agreement import _best_metric
    from dps.skills.covenants.ledger import table_blocks
    from dps.skills.covenants.names import canonical

    out = []
    for p in pages:
        for block in table_blocks(p.get("text") or ""):
            metric = _best_metric(block["lead"]) or canonical(None, block["lead"]) or "OTHER"
            op = "<=" if CEILING.search(block["lead"]) else ">=" if FLOOR.search(block["lead"]) else None
            levels = [{"from": r["date"], "to": None, "threshold": r["value"]} for r in block["rows"]]
            value = {"metric": metric, "operator": op, "threshold": levels[0]["threshold"], "unit": "TIMES",
                     "expression": None, "schedule": levels}
            f = make_field("covenants", pages, "FINANCIAL_COVENANT", metric, value, int(p["no"]), block["quote"],
                           "RULES", 0.6, [issue("STEP_TABLE", f"{len(levels)} levels read from a table; check each "
                                                              "date and level against the page")],
                           key=f"{metric}-table-{p['no']}-{block['start']}")
            f["status"] = "NEEDS_REVIEW"
            out.append(f)
    return out


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    return checks_for(fields)
