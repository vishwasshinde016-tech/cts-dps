"""A document's history (``dps_trace_event``): what DPS read and found, what the AI reader said and what people
decided, in order. Shown in the History section of the document and written as a line in the daily log.

    UPLOAD   rm-anil uploaded "Facility Agreement.pdf" for CP-1
    READ     86 pages: 84 text, 2 OCR (standard reader)
    FOUND    DSCR >= 1.25 (page 42) "shall maintain a DSCR of not less than 1.25 times"
    SHOWN    possible covenant (page 44) "Debt Yield shall be at least 9%"
    FLAG     Debt/EBITDA: SEVERAL_LEVELS: the clause also gives 3.25
    AI       the AI reader confirmed 2, found 1 the rules did not
    RESULT   NEEDS_REVIEW: 1 flag, 1 possible covenant
    REVIEW   approver-1 edited Debt/EBITDA: DEBT_TO_EBITDA <= 3.50 -> DEBT_TO_EBITDA <= 3.25
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from dps.models import TraceEvent
from dps.ops import observability as obs

DPS = "dps"
METHOD_NAMES = {"TEXT": "text", "OCR": "OCR", "DOCLING": "Docling", "BLANK": "blank", "NONE": "unreadable"}


def add(db: Session, document_id: str, step: str, summary: str, *, actor: str = DPS, job_id: str | None = None,
        trace_id: str | None = None, **detail: Any) -> None:
    """One step of the document's history (the caller commits), and the same as a line in the log. ``trace_id``
    (default: the current trace) links the row to its trace in Langfuse."""
    summary = summary[:1000]
    db.add(TraceEvent(document_id=document_id, job_id=job_id, step=step, actor=actor, summary=summary,
                      detail_json=json.dumps(detail, ensure_ascii=False, default=str),
                      trace_id=trace_id or obs.current_ids()[0]))
    with obs.bound(document_id=document_id, job_id=job_id):
        obs.event(step, summary, actor=actor, **{k: v for k, v in detail.items() if isinstance(v, (str, int, float))})


def _limit(p: dict) -> str:
    if p.get("operator") is None or p.get("threshold") is None:
        return ""
    unit = f" {p['unit']}" if p.get("unit") and p["unit"] not in ("x", "%") else (p.get("unit") or "")
    return f" {p['operator']} {p['threshold']:g}{unit}"


def _quote(text: str | None) -> str:
    text = " ".join((text or "").split())
    return f' "{text[:160]}{"..." if len(text) > 160 else ""}"' if text else ""


def result(db: Session, document_id: str, job_id: str | None, outcome: str, reason: str = "") -> None:
    add(db, document_id, "RESULT", outcome + (f": {reason}" if reason else ""), job_id=job_id, outcome=outcome)

