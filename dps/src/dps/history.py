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
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps import observability as obs
from dps.models import AiCall, TraceEvent

DPS = "dps"
METHOD_NAMES = {"TEXT": "text", "OCR": "OCR", "DOCLING": "Docling", "BLANK": "blank", "NONE": "unreadable"}


def add(db: Session, document_id: str, step: str, summary: str, *, actor: str = DPS, job_id: str | None = None,
        **detail: Any) -> None:
    """One step of the document's history (the caller commits), and the same as a line in the log."""
    summary = summary[:1000]
    db.add(TraceEvent(document_id=document_id, job_id=job_id, step=step, actor=actor, summary=summary,
                      detail_json=json.dumps(detail, ensure_ascii=False, default=str)))
    with obs.bound(document_id=document_id, job_id=job_id):
        obs.event(step, summary, actor=actor, **{k: v for k, v in detail.items() if isinstance(v, (str, int, float))})


def pages_read(db: Session, document_id: str, job_id: str | None, pages: list[dict], heavy: bool) -> None:
    methods = Counter(p.get("method", "TEXT") for p in pages)
    parts = ", ".join(f"{n} {METHOD_NAMES.get(m, m)}" for m, n in methods.most_common())
    reader = "Docling reader" if heavy else "standard reader"
    add(db, document_id, "READ", f"{len(pages)} pages: {parts or 'none'} ({reader})", job_id=job_id,
        pages=len(pages), methods=dict(methods), heavy=heavy)


def _limit(p: dict) -> str:
    if p.get("operator") is None or p.get("threshold") is None:
        return ""
    unit = f" {p['unit']}" if p.get("unit") and p["unit"] not in ("x", "%") else (p.get("unit") or "")
    return f" {p['operator']} {p['threshold']:g}{unit}"


def _quote(text: str | None) -> str:
    text = " ".join((text or "").split())
    return f' "{text[:160]}{"..." if len(text) > 160 else ""}"' if text else ""


def agreement_found(db: Session, document_id: str, job_id: str | None, proposals: list[dict]) -> None:
    """Each covenant found (or shown as possible), its flags, and what the AI reader did."""
    for p in proposals:
        metric = p.get("metric_raw") or ""
        name = p.get("name") or metric
        if name == metric.replace("_", " ").title():
            name = metric  # "ICR", not "Icr"
        page = f" (page {p['page']})" if p.get("page") else ""
        if p.get("category") == "CANDIDATE":
            add(db, document_id, "SHOWN", f"possible covenant{page}{_quote(p.get('quote'))}", job_id=job_id,
                page=p.get("page"))
        else:
            kind = "" if p.get("category") == "FINANCIAL" else " (non-financial)"
            add(db, document_id, "FOUND", f"{name}{_limit(p)}{kind}{page}{_quote(p.get('quote'))}", job_id=job_id,
                metric=p.get("metric_raw"), operator=p.get("operator"), threshold=p.get("threshold"),
                unit=p.get("unit"), page=p.get("page"), method=p.get("method"), confidence=p.get("confidence"))
        for issue in p.get("issues") or []:
            if not issue.startswith("POSSIBLE_COVENANT"):
                add(db, document_id, "FLAG", f"{name}: {issue}", job_id=job_id, metric=p.get("metric_raw"))
        obs.count("dps.items.found", 1, document_type="LOAN_AGREEMENT",
                  kind="possible" if p.get("category") == "CANDIDATE" else "covenant", metric=p.get("metric_raw"))
    methods = Counter(p.get("method") for p in proposals)
    if methods["REGEX+AI"] or methods["AI"]:
        add(db, document_id, "AI", f"the AI reader confirmed {methods['REGEX+AI']}, found {methods['AI']} the rules "
            "did not (each one held for a person)", job_id=job_id, confirmed=methods["REGEX+AI"],
            ai_only=methods["AI"])


def statement_found(db: Session, document_id: str, job_id: str | None, values: list[dict], likely: list[dict],
                    issues: list[str], confirmed: list[str], unit_scale: str) -> None:
    """Each figure read (in the loan's money unit, with its line and page), the inputs not found, and the flags."""
    if unit_scale:
        from dps.money import currency_of

        unit = next((v.get("unit") for v in values if currency_of(v.get("unit"))), "INR_CR")
        stored = "INR crore" if unit == "INR_CR" else f"{currency_of(unit)} million"
        add(db, document_id, "READ", f"statement in {unit_scale}; figures stored in {stored}", job_id=job_id,
            unit=unit_scale)
    for v in values:
        how = "derived" if v.get("derived") else f'line "{v.get("label") or "?"}"'
        page = f", page {v['page']}" if v.get("page") else ""
        add(db, document_id, "VALUE", f"{v['key']} = {v['value']:,.2f} ({how}{page})", job_id=job_id,
            key=v["key"], value=v["value"], page=v.get("page"), derived=bool(v.get("derived")))
        obs.count("dps.items.found", 1, document_type="FINANCIAL_STATEMENT", kind="value", metric=v["key"])
    for x in likely:
        add(db, document_id, "MISSING", f"{x.get('key')} not found; possible line{_quote(x.get('label'))}",
            job_id=job_id, key=x.get("key"))
    for issue in issues:
        add(db, document_id, "FLAG", issue, job_id=job_id)
    if confirmed:
        add(db, document_id, "AI", f"the AI reader read the same value for {', '.join(confirmed)}", job_id=job_id,
            confirmed=len(confirmed))


def result(db: Session, document_id: str, job_id: str | None, outcome: str, reason: str = "") -> None:
    add(db, document_id, "RESULT", outcome + (f": {reason}" if reason else ""), job_id=job_id, outcome=outcome)


def for_document(db: Session, document_id: str, with_text: bool) -> dict:
    """The History section: the steps in order, and every model call for the document (its text for roles that
    may read prompts: administrators, auditors and support)."""
    steps = db.scalars(select(TraceEvent).where(TraceEvent.document_id == document_id)
                       .order_by(TraceEvent.at, TraceEvent.id)).all()
    calls = db.scalars(select(AiCall).where(AiCall.document_id == document_id).order_by(AiCall.at)).all()
    return {
        "steps": [{"at": s.at, "step": s.step, "actor": s.actor, "summary": s.summary, "jobId": s.job_id,
                   "detail": json.loads(s.detail_json or "{}")} for s in steps],
        "aiCalls": [{"at": c.at, "purpose": c.purpose, "user": c.user, "model": c.model,
                     "promptVersion": c.prompt_version, "status": c.status, "error": c.error, "seconds": c.seconds,
                     "promptTokens": c.prompt_tokens, "completionTokens": c.completion_tokens, "cost": c.cost,
                     "requestText": c.request_text if with_text else None,
                     "responseText": c.response_text if with_text else None} for c in calls],
    }
