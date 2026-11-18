"""Cycle obligations (spec Flow 3): "validate reporting period, legal entity, consolidation basis, units/currency and
evidence type against cycle obligations; route mismatch to review".

Spring sends what the cycle expects in the job context (``asOfDate``, ``basis``, ``legalEntity``, ``expectedCurrency``,
``expectedUnits``, ``evidenceType``). Each value the document shows differently is a blocking warning with the page and
the line, and the job goes to a person. A value the document does not show is never guessed (the required-fields check
reports it). Entity and type problems are mapping problems: the result's route is ``MAPPING_REVIEW``.
"""

from __future__ import annotations

import re

from dps.analyze.identify import Found, name_similarity, normalise_name

CIN_SHAPE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
NAME_MATCH = 0.85
MAPPING_CODES = frozenset({"ENTITY_MISMATCH", "TYPE_MISMATCH", "MAPPING_CONFLICT"})
CODES = ("PERIOD_MISMATCH", "BASIS_MISMATCH", "ENTITY_MISMATCH", "CURRENCY_MISMATCH", "UNITS_MISMATCH")


def _warn(code: str, message: str, f: Found | None = None) -> dict:
    out = {"code": code, "message": message}
    if f is not None and f.page:
        out["source"] = {"page": f.page, "quote": (f.quote or "")[:300] or None}
    return out


def _first(found: list[Found], kind: str) -> list[Found]:
    return [f for f in found if f.kind == kind]


def check(ctx: dict, found: list[Found], basis: str | None = None, model: dict | None = None) -> list[dict]:
    """Warnings for every cycle expectation in ``ctx`` that the document contradicts (``model``: the page model, for
    the company name in a statement's heading)."""
    out: list[dict] = []
    found = [*found, *header_names(model)]
    expected = str(ctx.get("asOfDate") or "")[:10]
    periods = _first(found, "PERIOD_END")
    if expected and periods and all(p.value != expected for p in periods):
        out.append(_warn("PERIOD_MISMATCH", f"the cycle expects the period ending {expected}; the document shows "
                                            f"{periods[0].value}", periods[0]))
    want_basis = ctx.get("basis")
    if want_basis and basis and basis != want_basis:
        out.append(_warn("BASIS_MISMATCH", f"the cycle expects {want_basis} figures; the document reads as {basis}"))
    entity = str(ctx.get("legalEntity") or "").strip()
    if entity:
        out += _entity(entity, found)
    currency = str(ctx.get("expectedCurrency") or "").upper()
    shown = _first(found, "CURRENCY")
    if currency and shown and shown[0].value != currency:
        out.append(_warn("CURRENCY_MISMATCH", f"the cycle expects {currency}; the document is in {shown[0].value}",
                         shown[0]))
    units = str(ctx.get("expectedUnits") or "").upper()
    shown_units = _first(found, "UNIT")
    if units and shown_units and all(u.value != units for u in shown_units):
        out.append(_warn("UNITS_MISMATCH", f"the cycle expects amounts in {units}; the document states "
                                           f"{shown_units[0].value}", shown_units[0]))
    return out


COMPANY_LINE = re.compile(r"^\s*([A-Z][A-Za-z0-9&.,'\- ]{2,80}?(?:Private\s+Limited|Pvt\.?\s+Ltd\.?|Limited|Ltd\.?|"
                          r"LLP|PLC|Inc\.?))\s*$", re.M)


def header_names(model: dict | None) -> list[Found]:
    """A statement or certificate names its company in its heading lines (no "Borrower" wording): the company-name
    lines among the first lines of the first page."""
    pages = (model or {}).get("pages") or []
    if not pages:
        return []
    text = "\n".join((pages[0].get("text") or "").splitlines()[:6])
    return [Found("BORROWER_NAME", m.group(1).strip(), pages[0].get("no", 1), m.group(0).strip())
            for m in COMPANY_LINE.finditer(text)]


def _entity(entity: str, found: list[Found]) -> list[dict]:
    if CIN_SHAPE.match(entity.upper()):
        cins = _first(found, "CIN")
        if cins and all(c.value.upper() != entity.upper() for c in cins):
            return [_warn("ENTITY_MISMATCH", f"the cycle is for CIN {entity}; the document shows CIN {cins[0].value}",
                          cins[0])]
        return []
    names = _first(found, "BORROWER_NAME")
    if not names or not normalise_name(entity):
        return []
    if any(name_similarity(entity, n.value) >= NAME_MATCH for n in names):
        return []
    return [_warn("ENTITY_MISMATCH", f"the cycle is for {entity}; the document names {names[0].value}", names[0])]


def route_for(warnings: list[dict]) -> str:
    """MAPPING_REVIEW when a warning is a mapping problem (wrong entity, wrong type, identifiers of another
    borrower), else NEEDS_REVIEW."""
    return "MAPPING_REVIEW" if any(w.get("code") in MAPPING_CODES for w in warnings) else "NEEDS_REVIEW"
