"""Segmentation rules (Phase 2): split a PDF that bundles several documents into page ranges.

A new part starts at a page whose top lines carry a document heading (SCHEDULE, ANNEXURE, COMPLIANCE
CERTIFICATE, BALANCE SHEET AS AT, INDEPENDENT AUDITOR'S REPORT ...), or where the printed page number restarts
at 1. A split backed by only one weak signal is marked ``uncertain`` for the phase 3 agent skill.
"""

from __future__ import annotations

import re

HEADINGS = [
    (re.compile(r"^\s*(the\s+)?(first|second|third|fourth|fifth|\d+(st|nd|rd|th)?)?\s*schedule\b", re.I), "SCHEDULE"),
    (re.compile(r"^\s*annexure\b|^\s*annex\s+[a-z0-9]", re.I), "ANNEXURE"),
    (re.compile(r"compliance\s+certificate", re.I), "COMPLIANCE_CERTIFICATE"),
    (re.compile(r"balance\s+sheet\s+as\s+(at|on)", re.I), "FINANCIAL_STATEMENT"),
    (re.compile(r"independent\s+auditor'?s?\s*'?\s*report", re.I), "AUDIT_REPORT"),
    (re.compile(r"statement\s+of\s+profit\s+and\s+loss", re.I), "FINANCIAL_STATEMENT"),
    (re.compile(r"^\s*(facility|loan|term\s+loan|credit)\s+agreement\b", re.I), "LOAN_AGREEMENT"),
    (re.compile(r"^\s*sanction\s+letter\b", re.I), "SANCTION_LETTER"),
    (re.compile(r"^\s*(deed\s+of\s+)?(guarantee|hypothecation)\b", re.I), "SECURITY_DOCUMENT"),
    (re.compile(r"^\s*amendment\s+(agreement|letter)\b", re.I), "AMENDMENT"),
]
PAGE_ONE = re.compile(r"^\s*(page\s+1(\s+of\s+\d+)?|-\s*1\s*-|1\s*/\s*\d+)\s*$", re.I)
TOP_LINES = 6


def _top(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()][:TOP_LINES]


def _heading(text: str) -> str | None:
    for line in _top(text):
        for pattern, kind in HEADINGS:
            if pattern.search(line):
                return kind
    return None


def _restarts(text: str) -> bool:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    edges = lines[:2] + lines[-2:]
    return any(PAGE_ONE.match(line) for line in edges)


def segment(model: dict) -> list[dict]:
    """Page ranges for a PDF page model; one segment for any other format."""
    pages = model.get("pages", [])
    if not pages:
        return []
    if model.get("format") != "PDF" or len(pages) == 1:
        return [{"fromPage": 1, "toPage": len(pages), "type": _heading(pages[0].get("text", "")),
                 "uncertain": False}]
    starts: list[tuple[int, str | None, bool]] = [(1, _heading(pages[0].get("text", "")), False)]
    for p in pages[1:]:
        kind = _heading(p.get("text", ""))
        restart = _restarts(p.get("text", ""))
        if kind and kind != starts[-1][1]:
            starts.append((p["no"], kind, not restart and kind in ("SCHEDULE", "ANNEXURE")))
        elif restart and not kind:
            starts.append((p["no"], None, True))
    out = []
    for i, (first, kind, uncertain) in enumerate(starts):
        last = starts[i + 1][0] - 1 if i + 1 < len(starts) else pages[-1]["no"]
        out.append({"fromPage": first, "toPage": last, "type": kind, "uncertain": uncertain})
    return out
