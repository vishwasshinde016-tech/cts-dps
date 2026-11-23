"""certificate pack rules: certified figure and declared compliance per covenant, signatory, designation, date and
UDIN. Figures are returned beside the statement figures; Spring compares them (DPS gives no verdict)."""

from __future__ import annotations

import re
from datetime import date

from dps.analyze.identify import UDIN, udin_valid
from dps.extraction.agreement import _best_metric
from dps.skills.common import amount, issue, make_field

PACK = "certificate"
FIGURE = re.compile(r"(?<![\w.])(\d{1,6}(?:,\d{2,3})*(?:\.\d+)?)\s*(x|times|%)?(?![\w.])", re.I)
COMPLIED = re.compile(r"\b(not\s+complied|non[\s-]*compli\w*|breach(ed)?|not\s+met|no)\b|"
                      r"\b(complied|compliant|in\s+compliance|met|yes|satisfied)\b", re.I)
DESIGNATION = re.compile(r"\b(partner|proprietor|director|managing\s+director|chief\s+financial\s+officer|cfo|"
                         r"company\s+secretary|authori[sz]ed\s+signatory)\b", re.I)
NAME_LINE = re.compile(r"^\s*(?:(?i:name)\s*[:\-]\s*|\(?\s*(?i:ca\.?|mr\.?|ms\.?|shri)\s+)"
                       r"([A-Z][A-Za-z.\s]{2,60}?)\s*\)?\s*$", re.M)
DATE = re.compile(r"\bdate[d]?\s*[:\-]?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", re.I)
UNITS = {"x": "TIMES", "times": "TIMES", "%": "PERCENT"}
# A certificate written as a sentence lists several ratios on one line: "the Historic DSCR was 2.22; (ii) the
# Projected DSCR was 1.98; and (iii) the Loan Life Cover Ratio as 9.05 : 1" (Herefordshire, bulk run of 22 Nov 2026:
# the DSCR was read as 9.05). Each listed item keeps its own number; a table row has no such separators.
_ITEMS = re.compile(r";|\(\s*(?:[ivx]{1,4}|[a-h])\s*\)")
_TO_ONE = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*1(?:\.0+)?(?![\d.])")  # "9.05 : 1" is 9.05


def _segments(line: str) -> list[str]:
    return [s for s in _ITEMS.split(line) if s.strip()]


def _signed(pages: list[dict]) -> dict:
    out = {"signatory": None, "designation": None, "signedOn": None, "udin": None}
    for p in reversed(pages):
        text = p.get("text") or ""
        if out["udin"] is None:
            m = UDIN.search(text)
            if m and udin_valid(m.group(1).upper()):
                out["udin"] = m.group(1).upper()
        if out["designation"] is None and (m := DESIGNATION.search(text)):
            out["designation"] = m.group(1).title()
        if out["signatory"] is None and (m := NAME_LINE.search(text)):
            out["signatory"] = m.group(1).strip()
        if out["signedOn"] is None and (m := DATE.search(text)):
            try:
                out["signedOn"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            except ValueError:
                pass
    return out


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    signed = _signed(pages)
    out = []
    seen = set()
    for p in pages:
        lines = [s for whole in (p.get("text") or "").splitlines() for s in _segments(_TO_ONE.sub(r"\1", whole))]
        for line in lines:
            metric = _best_metric(line)
            if not metric or metric in seen:
                continue
            figures = [m for m in FIGURE.finditer(line) if not re.fullmatch(r"(19|20)\d\d", m.group(1))]
            if not figures:
                continue
            fig = figures[-1]
            # The certified (actual) figure is the last number on the line; a required level comes before it.
            declared = None
            c = COMPLIED.search(line.split(fig.group(0))[-1]) or COMPLIED.search(line)
            if c:
                declared = c.group(3) is not None
            seen.add(metric)
            unit = UNITS.get((fig.group(2) or "").lower())
            value = {"covenant": metric, "figure": amount(fig.group(1)), "unit": unit,
                     "declaredCompliant": declared, **signed}
            issues = [] if declared is not None else [issue("NO_DECLARATION", "no complied / not complied wording")]
            f = make_field(PACK, pages, "CERTIFIED_FIGURE", metric, value, p["no"], line.strip(),
                           confidence=0.85 if declared is not None else 0.6, issues=issues)
            if issues:
                f["status"] = "NEEDS_REVIEW"
            out.append(f)
    return out


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    warnings = []
    if fields and not any(f["value"].get("udin") for f in fields):
        warnings.append(issue("UDIN_MISSING", "no valid UDIN on the certificate"))
    if fields and not any(f["value"].get("signatory") for f in fields):
        warnings.append(issue("SIGNATORY_MISSING", "signatory name not read"))
    if not fields:
        warnings.append(issue("NOTHING_FOUND", "no certified covenant figures found"))
    return warnings
