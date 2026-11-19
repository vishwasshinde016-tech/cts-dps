"""Taxonomy codes on result fields (18 Nov 2026).

Every covenant-like field (covenants, certified figures, amended covenants) gets ``value.code`` and
``value.category`` from the covenant taxonomy, next to the DPS name it already has in ``value.metric``. Where the
document does not say a quantitative covenant's operator, unit or test frequency, the field stays empty and the
taxonomy's usual value is offered in ``value.suggested`` (never written into the field), with ``TAXONOMY_DEFAULT``,
for a person to confirm. Events of default become ``EVENT_OF_DEFAULT`` fields of their own, always for a person to
confirm.
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
    """Sets value.code and value.category; suggests taxonomy defaults on quantitative covenants (``defaults``)."""
    from dps.skills.common import issue

    tax = taxonomy()
    for f in fields:
        if f.get("group") not in STAMPED or not isinstance(f.get("value"), dict):
            continue
        v = f["value"]
        code = code_for_field(f)
        narrower = tax.more_specific((f.get("source") or {}).get("quote"), code)
        if narrower:  # "First Lien Leverage Ratio" is not total leverage: the quote names the narrower test
            code = narrower
            v["metric"] = tax.dps_key(code) or code
        if tax.category_of(code) not in RESULT_CATEGORIES:
            code = None  # richer obligation roles have no representation in the v1 covenant field schema
        v["code"] = code
        v["category"] = tax.category_of(code)
        t = tax.get(code)
        if not defaults or t is None or not t.quantitative or f.get("group") not in COVENANT_GROUPS:
            continue
        # What the document does not state stays empty. The taxonomy's usual value is only a suggestion beside it
        # (``value.suggested``), so an unconfirmed default can never be applied as if the document said it
        # (19 Nov 2026 decision; the Codex profiles' "never fill from typical-market assumptions").
        suggested = {}
        if v.get("threshold") is not None:
            if not v.get("operator") and t.default_operator:
                suggested["operator"] = t.default_operator
            if not v.get("unit") and (t.default_operator or t.frequency) and UNITS.get(t.unit or ""):
                suggested["unit"] = UNITS[t.unit]
            test = f.get("test") if isinstance(f.get("test"), dict) else None
            if t.frequency and not (test or {}).get("frequency"):
                suggested["frequency"] = FREQUENCIES.get(t.frequency, str(t.frequency).upper())
        if suggested:
            v["suggested"] = suggested
            f.setdefault("issues", []).append(issue(
                "TAXONOMY_DEFAULT", f"not stated in the document: {', '.join(suggested)}; the taxonomy's usual "
                                    f"value for {t.name} is suggested, a person confirms or corrects it"))
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
