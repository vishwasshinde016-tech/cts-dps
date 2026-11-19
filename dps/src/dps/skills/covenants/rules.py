"""covenants pack rules: today's agreement extractor (agreement.py + safety net), as dps.result fields."""

from __future__ import annotations

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
            "expression": None, "schedule": schedule}


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


def checks_for(fields: list[dict]) -> list[dict]:
    warnings = []
    for f in fields:
        v = f["value"]
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
    from dps.extraction.coverage import possible_covenants

    read = as_pages(pages)
    terms = extract_terms(read)
    candidates, _ = possible_covenants(read, terms)
    return [term_field("covenants", pages, t, key=f"{t.metric}-{i}") for i, t in enumerate(terms + candidates)]


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    return checks_for(fields)
