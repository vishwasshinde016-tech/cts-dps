"""The covenant as a condition in the bank app's formula language (CTS integration, 23 Nov 2026).

The bank app (covenant-service) takes ``value.expression`` as the per-CP condition of a covenant it applies. Its
formula language is ``MEASURE OP NUMBER`` with the app's own measure codes (``INTEREST_COVER``, ``NET_LEVERAGE`` ...),
``==`` for equality, plain decimal numbers, amounts in INR crore and percentages as points. So:

- the expression always carries the **document's** operator and threshold (never the library master's formula; that
  one stays in ``libraryMatch.expression`` for comparison);
- the measure is the app's code from the library match (its ``metricKeys`` or its own formula), else DPS's metric;
- no expression is written for an unknown metric (OTHER), an unreadable operator, a missing threshold or an amount
  that cannot be expressed in INR crore;
- ``value.mapping`` says whether one plain formula is the whole covenant (``mappable``) and, when not, why: a condition,
  a qualifier, another obligor, a step-down schedule, an amount in another currency, a formula the app refused.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from dps.skills.common import issue

# Operators the contract allows, from what the rules and the agent write.
_OPERATORS = {
    ">=": ">=", "≥": ">=", "=>": ">=", "min": ">=", "minimum": ">=", "at least": ">=", "not less than": ">=",
    "no less than": ">=", "floor": ">=",
    "<=": "<=", "≤": "<=", "=<": "<=", "max": "<=", "maximum": "<=", "at most": "<=", "not more than": "<=",
    "no more than": "<=", "not exceed": "<=", "shall not exceed": "<=", "ceiling": "<=", "cap": "<=",
    ">": ">", "more than": ">", "greater than": ">", "above": ">", "exceeds": ">", "in excess of": ">",
    "<": "<", "less than": "<", "below": "<",
    "=": "=", "==": "=", "equal": "=", "equals": "=", "equal to": "=",
}
APP_OPERATOR = {">=": ">=", ">": ">", "<=": "<=", "<": "<", "=": "=="}
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_LEADING_MEASURE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:>=|<=|==|!=|>|<)")
SCALE = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
         "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "rupee": 1.0, "rupees": 1.0}
AMOUNT_METRICS = {"MIN_TANGIBLE_NET_WORTH", "MIN_NET_WORTH", "MINIMUM_LIQUIDITY", "MIN_EBITDA",
                  "MIN_COLLATERAL_VALUE", "ASSET_DISPOSAL_CAP"}
PERCENT_UNITS = {"%", "PERCENT", "PCT"}
BORROWER = {None, "", "Borrower", "Borrowers", "Company", "Issuer"}
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
                 "%d %B, %Y", "%d/%m/%Y", "%d-%m-%Y")


def normalise_operator(op) -> str | None:
    """The contract's operator (>=, >, <=, <, =) for what a reader wrote, or None when it is not one."""
    if op is None:
        return None
    key = re.sub(r"\s+", " ", str(op)).strip().lower()
    return _OPERATORS.get(key)


def plain_number(value: float | int) -> str:
    """1.25 -> "1.25", 5000000 -> "5000000", 3.0 -> "3": never exponent notation (the app's lexer has none)."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    d = Decimal(repr(float(value))) if isinstance(value, float) else Decimal(int(value))
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def usable_metric(metric) -> bool:
    """A metric a formula can name: an identifier, not OTHER / *_OTHER, not a phrase as written."""
    if not isinstance(metric, str) or not _IDENT.match(metric):
        return False
    upper = metric.upper()
    return upper != "OTHER" and not upper.endswith("_OTHER")


def parse_date(text) -> date | None:
    if isinstance(text, date):
        return text
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", " ".join(str(text or "").split()))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    m = re.fullmatch(r"Q([1-4])\s*(\d{4})", raw, re.I)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        return date(y, 3 * q, 31 if q in (1, 4) else 30)
    return None


def level_in_force(schedule, as_of: date | None) -> tuple[float | None, str | None]:
    """The threshold of a step-down schedule in force on ``as_of`` (the last level that started on or before it),
    with the date it started; the first level when no date can be read or none has started yet."""
    levels = [lv for lv in schedule or [] if isinstance(lv, dict) and isinstance(lv.get("threshold"), (int, float))
              and not isinstance(lv.get("threshold"), bool)]
    if not levels:
        return None, None
    as_of = as_of or date.today()
    dated = [(parse_date(lv.get("from")), lv) for lv in levels]
    started = [(d, lv) for d, lv in dated if d is not None and d <= as_of]
    if started:
        d, lv = max(started, key=lambda x: x[0])
        return float(lv["threshold"]), d.isoformat()
    return float(levels[0]["threshold"]), None


def _inr_crore(value: dict) -> tuple[float | None, str | None]:
    """An amount threshold in INR crore, or (None, reason) when it cannot be written so."""
    threshold = value.get("threshold")
    unit = str(value.get("unit") or "").strip().lower().rstrip(".")
    currency = (value.get("currency") or "").upper() or None
    if unit in ("inr_cr", "inr cr"):
        return float(threshold), None
    if currency is None and unit in ("crore", "crores", "cr", "lakh", "lakhs", "lac", "lacs"):
        currency = "INR"  # crore and lakh are Indian scales
    if currency != "INR":
        return None, f"AMOUNT_CURRENCY: the amount is in {currency or 'an unknown currency'}; the app keeps amounts " \
                     "in INR crore"
    scale = SCALE.get(unit or "rupee")
    if scale is None:
        return None, f"AMOUNT_UNIT: '{unit}' is not a known scale"
    return float(threshold) * scale / 1e7, None


def is_amount(value: dict) -> bool:
    unit = str(value.get("unit") or "").strip().lower().rstrip(".")
    return (unit in SCALE or unit in ("inr_cr", "inr cr") or bool(value.get("currency"))
            or str(value.get("metric") or "").upper() in AMOUNT_METRICS)


# ---------------------------------------------------------------------------------------------------- library match

def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def library_index(items: list) -> dict[str, list[dict]]:
    """key -> library items (each once). Keys: name, aliases, metricKeys. Items without an id are skipped; null lists
    are empty lists (the app's JSON may send them)."""
    index: dict[str, list[dict]] = {}
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        keys = [item.get("name"), *(item.get("aliases") or []), *(item.get("metricKeys") or [])]
        for key in keys:
            k = _norm(key)
            if k and all(x["id"] != item["id"] for x in index.get(k, [])):
                index.setdefault(k, []).append(item)
    return index


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) > 2}


def match_library(f: dict, index: dict[str, list[dict]]) -> tuple[dict | None, list[dict]]:
    """(the library item for this covenant, or None; the candidates when several fit equally). Looked up by the
    taxonomy code, the metric, the other codes read for the same sentence, then the name. When one key fits several
    library items (two masters on DSCR), the one whose name shares most words with the covenant's name wins; a tie is
    left to a person."""
    value = f.get("value") or {}
    keys = [value.get("code"), value.get("metric"), *(value.get("alsoCodes") or []), f.get("name")]
    for key in keys:
        found = index.get(_norm(key)) if key else None
        if not found:
            continue
        if len(found) == 1:
            return found[0], []
        mine = _words(f.get("name")) | _words(value.get("metric"))
        scored = sorted(((len(mine & _words(x.get("name"))), x) for x in found), key=lambda s: -s[0])
        if scored[0][0] > scored[1][0]:
            return scored[0][1], []
        return None, [x for s, x in scored if s == scored[0][0]]
    return None, []


def app_measure(f: dict, hit: dict | None) -> str | None:
    """The app's measure code for this covenant: the library item's single metric key, the one equal to DPS's metric,
    the measure its own formula starts with; DPS's metric when there is no library match."""
    metric = (f.get("value") or {}).get("metric")
    if hit:
        keys = [k for k in hit.get("metricKeys") or [] if isinstance(k, str) and _IDENT.match(k)]
        if len(keys) == 1:
            return keys[0]
        same = [k for k in keys if _norm(k) == _norm(metric)]
        if same:
            return same[0]
        m = _LEADING_MEASURE.match(str(hit.get("expression") or ""))
        if m:
            return m.group(1)
    return metric if usable_metric(metric) else None


# ---------------------------------------------------------------------------------------------------- the formula

def build(f: dict, hit: dict | None, as_of: date | None = None) -> dict:
    """Sets ``value.expression`` (or None) and ``value.mapping`` on one covenant field; returns the mapping."""
    value = f["value"]
    reasons: list[str] = []
    notes: list[str] = []
    op = normalise_operator(value.get("operator"))
    if value.get("operator") is not None and op is None:
        reasons.append(f"OPERATOR: '{value.get('operator')}' is not an operator")
    if op is not None:
        value["operator"] = op
    threshold = value.get("threshold")
    schedule = value.get("schedule")
    if isinstance(schedule, list) and len([s for s in schedule if isinstance(s, dict)]) > 1:
        level, since = level_in_force(schedule, as_of)
        if level is not None:
            threshold = level
        reasons.append("STEP_DOWN: the threshold changes over time; the formula uses the level in force "
                       + (f"since {since}" if since else "(no date read: the first level)"))
    unit = str(value.get("unit") or "").strip().upper()
    measure = app_measure(f, hit)
    expression = None
    app_unit = None
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        threshold = None
    if measure is None:
        reasons.append("METRIC: no measure the app knows")
    elif op is None:
        reasons.append("OPERATOR: not stated")
    elif threshold is None:
        reasons.append("THRESHOLD: not stated")
    else:
        number = threshold
        if unit in PERCENT_UNITS:
            notes.append("PERCENT: written as points (75 means 75 %)")
        elif is_amount(value):
            number, why = _inr_crore({**value, "threshold": threshold})
            if why:
                reasons.append(why)
            else:
                app_unit = "INR_CR"
        if number is not None and not reasons:
            expression = f"{measure} {APP_OPERATOR[op]} {plain_number(number)}"
    if value.get("conditions"):
        reasons.append("CONDITIONAL: applies only when " + "; ".join(
            str(c.get("text") if isinstance(c, dict) else c) for c in value["conditions"])[:200])
    if value.get("qualifiers"):
        reasons.append("QUALIFIED: " + "; ".join(map(str, value["qualifiers"]))[:200])
    scope = value.get("scope") if isinstance(value.get("scope"), dict) else {}
    if scope.get("obligor") not in BORROWER:
        reasons.append(f"SCOPE: binds {scope['obligor']}, not the borrower")
    if scope.get("basis"):
        notes.append(f"BASIS: tested on {scope['basis'].lower()} figures")
    value["expression"] = expression
    mapping = {"mappable": expression is not None and not reasons, "reasons": reasons, "notes": notes,
               "measure": measure, "unit": app_unit}
    value["mapping"] = mapping
    return mapping


def apply(fields: list[dict], library: list | None, checks: dict | None = None, as_of: date | None = None) -> None:
    """Library match, formula and mapping for every covenant field (``checks``: the app's formula checks by field id,
    applied afterwards with :func:`record_checks`)."""
    from dps.skills.covenants.codes import COVENANT_GROUPS

    index = library_index(library or [])
    for f in fields:
        if f.get("group") not in COVENANT_GROUPS or not isinstance(f.get("value"), dict):
            continue
        hit, candidates = match_library(f, index) if index else (None, [])
        if hit:
            f["libraryMatch"] = {"covenantMasterId": hit["id"], "expression": hit.get("expression"),
                                 "name": hit.get("name")}
        elif candidates:
            f["libraryMatch"] = {"covenantMasterId": None, "expression": None,
                                 "candidates": [{"covenantMasterId": c["id"], "name": c.get("name")}
                                                for c in candidates]}
            f.setdefault("issues", []).append(issue(
                "LIBRARY_AMBIGUOUS", "several library covenants fit: " + ", ".join(
                    str(c.get("name") or c["id"]) for c in candidates)[:200] + "; a person picks one"))
            f["status"] = "NEEDS_REVIEW"
        build(f, hit, as_of)


def record_checks(fields: list[dict], checks: dict) -> None:
    """The app's verdict on each formula: an invalid one is not mappable, and the field goes to a person."""
    for f in fields:
        c = checks.get(f.get("id"))
        mapping = (f.get("value") or {}).get("mapping") if isinstance(f.get("value"), dict) else None
        if c is None or mapping is None:
            continue
        if not c.available:
            f["issues"].append(issue("SPRING_UNAVAILABLE", "formula not validated in Core"))
            f["status"] = "NEEDS_REVIEW"
        elif not c.valid:
            message = "; ".join(str(e.get("message") or e.get("code")) if isinstance(e, dict) else str(e)
                                for e in c.errors)[:300]
            f["issues"].append(issue("FORMULA_INVALID", message or "Core rejected the formula"))
            f["status"] = "NEEDS_REVIEW"
            mapping["mappable"] = False
            mapping["reasons"].append("FORMULA_INVALID: the app refused the formula")
