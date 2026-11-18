"""commentary pack: draft paragraphs from the outcome manifest only. Every number in the text must exist in the
manifest, and no compliance verdict goes beyond what the manifest says."""

from __future__ import annotations

import re

from dps.skills.common import issue

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
VERDICT = re.compile(r"\b(breach(?:ed|es)?|complied|compliant|non[\s-]*compli\w*|default(?:ed)?)\b", re.I)


def _numbers(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _numbers(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out |= _forms(float(obj))
    elif isinstance(obj, str):
        for m in NUM.finditer(obj):
            out |= _forms(float(m.group(0).replace(",", "")))
    return out


def _forms(x: float) -> set[str]:
    forms = {f"{x:.2f}", f"{x:.1f}", f"{x:.0f}", str(x)}
    if x == int(x):
        forms.add(str(int(x)))
    return forms


def text_numbers(text: str) -> list[str]:
    return [m.group(0) for m in NUM.finditer(text)]


def traceable(text: str, manifest: dict) -> list[str]:
    allowed = _numbers(manifest)
    missing = []
    for raw in text_numbers(text):
        x = float(raw.replace(",", ""))
        if not (_forms(x) & allowed):
            missing.append(raw)
    return missing


def draft(manifest: dict) -> list[str]:
    """A deterministic draft used when no model is available (and as the model's fallback)."""
    cp = manifest.get("cpName") or manifest.get("cpId") or "the borrower"
    period = manifest.get("period") or manifest.get("asOfDate") or "the period"
    rows = manifest.get("covenants") or []
    lines = [f"Covenant review for {cp} for {period}."]
    breached = [r for r in rows if str(r.get("result", "")).upper() == "BREACHED"]
    complied = [r for r in rows if str(r.get("result", "")).upper() in ("COMPLIANT", "COMPLIED")]
    pending = [r for r in rows if r not in breached and r not in complied]
    for label, group in (("complied", complied), ("breached", breached)):
        if group:
            parts = []
            for r in group:
                actual, req = r.get("actual"), r.get("threshold")
                bit = r.get("name", "covenant")
                if actual is not None:
                    bit += f" at {actual}"
                if req is not None:
                    bit += f" against {r.get('operator', '')} {req}".replace("  ", " ")
                parts.append(bit)
            lines.append(f"{len(group)} covenant(s) {label}: " + "; ".join(parts) + ".")
    if pending:
        lines.append(f"{len(pending)} covenant(s) await assessment: "
                     + ", ".join(r.get("name", "covenant") for r in pending) + ".")
    return lines


def check_text(text: str, manifest: dict) -> list[dict]:
    problems = []
    missing = traceable(text, manifest)
    if missing:
        problems.append(issue("NOT_GROUNDED", "numbers not in the manifest: " + ", ".join(missing[:5])))
    allowed_verdicts = {str(r.get("result", "")).upper() for r in manifest.get("covenants") or []}
    for m in VERDICT.finditer(text):
        word = m.group(1).lower()
        if word.startswith("breach") and "BREACHED" not in allowed_verdicts:
            problems.append(issue("VERDICT_NOT_IN_RESULTS", f"'{m.group(1)}' but no covenant is breached"))
        if word.startswith(("complied", "compliant")) and not allowed_verdicts & {"COMPLIANT", "COMPLIED"}:
            problems.append(issue("VERDICT_NOT_IN_RESULTS", f"'{m.group(1)}' but no covenant complied"))
    return problems


def rules(pages: list[dict], ctx: dict) -> list[dict]:
    return []
