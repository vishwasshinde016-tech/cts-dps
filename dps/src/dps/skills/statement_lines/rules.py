"""statement_lines pack: accounting identities and the restatement diff on statement fields (dps.result)."""

from __future__ import annotations

from dps.skills.common import issue

TOLERANCE = 0.005


def _by_period(fields: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for f in fields:
        if f["group"] != "STATEMENT_LINE" or f["value"].get("amount") is None:
            continue
        out.setdefault(f["value"].get("asOfDate") or "", {})[f["name"]] = float(f["value"]["amount"])
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b), 1.0)


def rules(pages: list[dict], ctx: dict) -> list[dict]:  # the legacy flow reads the lines
    return []


def checks(fields: list[dict], ctx: dict) -> list[dict]:
    warnings = []
    for period, v in _by_period(fields).items():
        liabilities = v.get("TOTAL_LIABILITIES")
        equity = v.get("NET_WORTH", v.get("TOTAL_EQUITY"))
        if "TOTAL_ASSETS" in v and liabilities is not None and equity is not None:
            if not _close(v["TOTAL_ASSETS"], liabilities + equity):
                warnings.append(issue("TOTALS_DO_NOT_ADD", f"{period}: assets {v['TOTAL_ASSETS']:,.2f} vs "
                                                           f"liabilities + equity {liabilities + equity:,.2f}"))
        if {"PBT", "TAX", "PAT"} <= v.keys() and not _close(v["PBT"] - abs(v["TAX"]), v["PAT"]):
            warnings.append(issue("TOTALS_DO_NOT_ADD", f"{period}: PBT - tax = {v['PBT'] - abs(v['TAX']):,.2f}, "
                                                       f"PAT {v['PAT']:,.2f}"))
    approved = ctx.get("approved") or {}
    for f in fields:
        old = approved.get((f["name"], f["value"].get("asOfDate")))
        new = f["value"].get("amount")
        if old is not None and new is not None and not _close(float(old), float(new)):
            f["issues"].append(issue("RESTATED", f"approved {old:,.2f}, this version {new:,.2f}"))
            f["status"] = "NEEDS_REVIEW"
    return warnings
