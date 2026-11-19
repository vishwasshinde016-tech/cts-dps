"""Taxonomy codes on result fields (18 Nov 2026).

Every covenant-like field (covenants, certified figures, amended covenants) gets ``value.code`` and
``value.category`` from the covenant taxonomy, next to the DPS name it already has in ``value.metric``. Where the
document does not say a quantitative covenant's operator, unit or test frequency, the taxonomy's default fills it,
the field says which in ``value.defaults``, carries ``TAXONOMY_DEFAULT`` and waits for a person. Events of default
become ``EVENT_OF_DEFAULT`` fields of their own, always for a person to confirm.
"""

from __future__ import annotations

from dps.skills.covenants.taxonomy import COVENANT_CATEGORIES
from dps.skills.covenants.taxonomy import get as taxonomy

COVENANT_GROUPS = {"FINANCIAL_COVENANT", "NON_FINANCIAL_COVENANT", "INFO_COVENANT", "POSSIBLE_COVENANT"}
STAMPED = COVENANT_GROUPS | {"CERTIFIED_FIGURE", "EVENT_OF_DEFAULT"}
RESULT_CATEGORIES = frozenset((*COVENANT_CATEGORIES, "event_of_default"))
UNITS = {"ratio": "TIMES", "multiple": "TIMES", "percent": "PERCENT"}
FREQUENCIES = {"annual": "YEARLY", "yearly": "YEARLY", "quarterly": "QUARTERLY", "monthly": "MONTHLY",
               "semi_annual": "HALF_YEARLY", "half_yearly": "HALF_YEARLY"}


def code_for_field(f: dict) -> str | None:
    tax = taxonomy()
    value = f.get("value") if isinstance(f.get("value"), dict) else {}
    for raw in (value.get("code"), value.get("metric"), value.get("covenant"), f.get("name")):
        code = tax.code_for(raw) if raw else None
        if code:
            return code
    return None


def stamp(fields: list[dict], defaults: bool = True) -> None:
    """Sets value.code and value.category; fills taxonomy defaults on quantitative covenants (``defaults``)."""
    from dps.skills.common import issue

    tax = taxonomy()
    for f in fields:
        if f.get("group") not in STAMPED or not isinstance(f.get("value"), dict):
            continue
        v = f["value"]
        code = code_for_field(f)
        if tax.category_of(code) not in RESULT_CATEGORIES:
            code = None  # richer obligation roles have no representation in the v1 covenant field schema
        v["code"] = code
        v["category"] = tax.category_of(code)
        t = tax.get(code)
        if not defaults or t is None or not t.quantitative or f.get("group") not in COVENANT_GROUPS:
            continue
        filled = []
        if v.get("threshold") is not None:
            if not v.get("operator") and t.default_operator:
                v["operator"] = t.default_operator
                filled.append("operator")
            if not v.get("unit") and (t.default_operator or t.frequency) and UNITS.get(t.unit or ""):
                v["unit"] = UNITS[t.unit]
                filled.append("unit")
            test = f.get("test") if isinstance(f.get("test"), dict) else None
            if t.frequency and not (test or {}).get("frequency"):
                f["test"] = {**(test or {"basis": None, "firstTestDate": None}),
                             "frequency": FREQUENCIES.get(t.frequency, str(t.frequency).upper())}
                filled.append("frequency")
        if filled:
            v["defaults"] = filled
            f.setdefault("issues", []).append(issue(
                "TAXONOMY_DEFAULT", f"not stated in the document; the taxonomy's usual {', '.join(filled)} for "
                                    f"{t.name} was used"))
            f["status"] = "NEEDS_REVIEW"
        elif v.get("threshold") is not None and (not v.get("operator") or not v.get("unit")):
            f.setdefault("issues", []).append(issue("REQUIRED_MISSING", "Confirm the operator and threshold unit."))
            f["status"] = "NEEDS_REVIEW"


def event_fields(pack_name: str, pages: list[dict], proposals: list[dict] | None) -> list[dict]:
    """The events of default the agent found (grounded), one per code and page, each for a person to confirm."""
    from dps.skills.common import issue, make_field

    tax = taxonomy()
    out, seen = [], set()
    for p in proposals or []:
        if not p.get("page") or not p.get("quote"):
            continue
        value = dict(p.get("value") or {})
        code = tax.code_for(value.get("metric")) or tax.code_for(p.get("name"))
        if tax.category_of(code) != "event_of_default":
            code = "EOD_OTHER"
        if (code, p["page"]) in seen:
            continue
        seen.add((code, p["page"]))
        value = {"code": code, "category": "event_of_default", "name": p.get("name"),
                 "threshold": value.get("threshold"), "unit": value.get("unit"),
                 "cureDays": value.get("cureDays")}
        f = make_field(pack_name, pages, "EVENT_OF_DEFAULT", str(p.get("name") or code), value, p["page"],
                       p["quote"], method="AI", confidence=0.7, key=f"eod-{code}")
        f["status"] = "NEEDS_REVIEW"
        f["issues"].append(issue("AI_ONLY", "found by the Document Agent; events of default are confirmed by a "
                                            "person"))
        out.append(f)
    return out
