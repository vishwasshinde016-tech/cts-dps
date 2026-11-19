"""Result adapter (Phase 1): today's covenant and statement rows as ``dps.result/1.0`` (contracts/schemas/result.json).

One run per job in Phase 1, so ``runId`` is the job id. Fields carry what the existing pipeline stored; values it
does not have yet (boxes, calibrated confidence, signals) are null rather than invented.
"""

from __future__ import annotations

import json

from sqlalchemy import false, select
from sqlalchemy.orm import Session

from dps.models import Document, DocumentCpLink, ExtractedTerm, Job, Run, StatementValue

SCHEMA = "dps.result/1.0"

TERM_GROUP = {"FINANCIAL": "FINANCIAL_COVENANT", "NON_FINANCIAL": "NON_FINANCIAL_COVENANT",
              "INFO": "INFO_COVENANT", "CANDIDATE": "POSSIBLE_COVENANT"}


def _issues(raw: str | None) -> list[dict]:
    out = []
    for item in json.loads(raw or "[]"):
        text = str(item)
        code, _, message = text.partition(":")
        out.append({"code": code.strip() or "ISSUE", "message": message.strip() or None})
    return out


def _term_status(term: ExtractedTerm) -> str:
    if term.status in ("NEEDS_REVIEW", "PROPOSED"):  # PROPOSED: waiting for a person's decision (F5)
        return "NEEDS_REVIEW"
    if term.status == "REJECTED":
        return "REJECTED"
    if term.status == "PUBLISHED" and term.reviewed_by:
        return "CONFIRMED"
    return "AUTO_ACCEPTED"


def term_field(term: ExtractedTerm) -> dict:
    return {
        "id": term.id,
        "group": TERM_GROUP.get(term.category, "INFO_COVENANT"),
        "name": term.name or term.metric_raw,
        "value": {"metric": term.metric_raw, "operator": term.operator, "threshold": term.threshold,
                  "unit": term.unit, "expression": term.expression,
                  "schedule": json.loads(term.schedule_json) if term.schedule_json else None},
        "source": {"page": term.page, "quote": term.quote, "bbox": None, "blockId": None, "cellRef": None},
        "test": {"frequency": term.frequency, "basis": None, "firstTestDate": None},
        "libraryMatch": {"covenantMasterId": term.covenant_master_id, "expression": term.expression},
        "method": term.method,
        "confidence": term.confidence,
        "signals": None,
        "issues": _issues(term.issues_json),
        "status": _term_status(term),
    }


def value_field(value: StatementValue, document_status: str) -> dict:
    return {
        "id": value.id,
        "group": "STATEMENT_LINE",
        "name": value.key,
        "value": {"amount": value.value, "unit": value.unit, "currency": value.currency,
                  "asOfDate": value.as_of_date.isoformat(), "periodMonths": value.period_months,
                  "derived": value.derived},
        "source": {"page": value.page, "quote": None, "bbox": None, "blockId": None, "cellRef": value.cell},
        "test": None,
        "libraryMatch": None,
        "method": value.source_type,
        "confidence": None,  # Phase 5 brings the calibrated score; nothing is invented before that
        "signals": None,
        "issues": [],
        "status": "NEEDS_REVIEW" if document_status == "NEEDS_REVIEW" else "AUTO_ACCEPTED",
    }


def latest_run(db: Session, job: Job) -> dict | None:
    run = db.scalars(select(Run).where(Run.job_id == job.id).order_by(Run.run_no.desc())).first()
    return json.loads(run.result_json) if run is not None else None


def approved_figures(db: Session, doc: Document | None) -> dict:
    """G4: the figures an earlier statement gave for the same borrower and period (the latest per line), to show
    what a restated statement changes. {(key, asOfDate): value}."""
    if doc is None or doc.as_of_date is None:
        return {}
    cps = [link.cp_id for link in db.scalars(select(DocumentCpLink).where(DocumentCpLink.document_id == doc.id))]
    if not cps:
        return {}
    rows = db.scalars(select(StatementValue).where(StatementValue.cp_id.in_(cps),
                                                   StatementValue.as_of_date == doc.as_of_date,
                                                   StatementValue.document_id != doc.id,
                                                   StatementValue.created_at < doc.uploaded_at,  # earlier ones only
                                                   StatementValue.source_type != "COMPARATIVE")
                      .order_by(StatementValue.created_at)).all()
    return {(r.key, r.as_of_date.isoformat()): r.value for r in rows}


def held_statement_fields(db: Session, job: Job, doc: Document) -> list[dict]:
    """A statement held for review keeps its figures in the flow's saved state (they reach the values table only
    after review). Show them, each NEEDS_REVIEW, with the flow's issues, so the reviewer sees what was read."""
    from dps.models import FlowState

    if not job.flow_state_id:
        return []
    row = db.scalars(select(FlowState).where(FlowState.flow_uuid == job.flow_state_id)
                     .order_by(FlowState.saved_at.desc(), FlowState.id.desc()).limit(1)).first()
    if row is None:
        return []
    state = json.loads(row.state_json or "{}")
    issues = _issues(json.dumps(state.get("issues") or []))
    as_of = doc.as_of_date.isoformat() if doc.as_of_date else None
    out = []
    for i, v in enumerate(state.get("values") or []):
        out.append({
            "id": f"held-{job.id[:8]}-{i}", "group": "STATEMENT_LINE", "name": v.get("key", ""),
            "value": {"amount": v.get("value"), "unit": v.get("unit"), "currency": None, "asOfDate": as_of,
                      "periodMonths": v.get("months"), "derived": bool(v.get("derived"))},
            "source": {"page": v.get("page"), "quote": v.get("label") or None, "bbox": None, "blockId": None,
                       "cellRef": v.get("cell")},
            "test": None, "libraryMatch": None, "method": "RULES", "confidence": None, "signals": None,
            "issues": issues, "status": "NEEDS_REVIEW"})
    return out


def coverage_of(job: Job) -> dict:
    """The agreement flow's candidate ledger and page outcomes, from its ``coverage`` job step (latest attempt)."""
    step = next((s for s in reversed(json.loads(job.steps_json or "[]")) if s.get("step") == "coverage"), None)
    if step is None:
        return {}
    if step.get("lines") is not None:  # a statement: the catalogue lines it did not yield
        return {"lines": step["lines"]}
    led = step.get("ledger") or {}
    out = {"coverage": {"dutySentences": led.get("candidates", 0), "explained": led.get("candidates", 0)
                        - led.get("unresolved", 0), "byQuote": led.get("finding", 0), "byKind": led.get("covered", 0),
                        "percent": led.get("percentExplained", 100.0), "unexplained": step.get("unresolved") or [],
                        "unexplainedCount": led.get("unresolved", 0), "ledger": led}}
    if step.get("pages"):
        out["pages"] = step["pages"]
    return out


def build(db: Session, job: Job, pipeline_version: str, store=None) -> dict:
    saved = latest_run(db, job)
    if saved is not None:  # phase 3+: the run stored its own dps.result/1.0
        return saved
    doc = db.get(Document, job.document_id)
    if job.flow == "STATEMENT_PROCESSING":
        values = db.scalars(select(StatementValue).where(StatementValue.document_id == job.document_id,
                                                         StatementValue.superseded == false())
                            .order_by(StatementValue.key, StatementValue.as_of_date)).all()
        fields = [value_field(v, doc.status if doc else "") for v in values]
        if not fields and doc is not None and (doc.status == "NEEDS_REVIEW" or job.rollout == "SHADOW"):
            fields = held_statement_fields(db, job, doc)
    else:
        terms = db.scalars(select(ExtractedTerm).where(ExtractedTerm.document_id == job.document_id)
                           .order_by(ExtractedTerm.page, ExtractedTerm.created_at)).all()
        fields = [term_field(t) for t in terms]
    # Phase 4 packs on the proven flows: schedule terms from the page model, statement identities.
    from dps.skills import runner as skills

    extra_fields, warnings = skills.legacy_extras(db, job, store)
    fields += extra_fields
    from dps.skills.covenants.codes import stamp
    from dps.skills.covenants.taxonomy import get as taxonomy

    stamp(fields, defaults=False)  # the proven flows: codes only, their review state stays as it was
    if job.flow == "STATEMENT_PROCESSING":
        warnings += skills.statement_warnings(fields, approved_figures(db, doc))
    if job.rollout == "ASSISTED":  # phase 7 pilot: analysts review every field
        for f in fields:
            if f["status"] == "AUTO_ACCEPTED":
                f["status"] = "NEEDS_REVIEW"
    covered = coverage_of(job)
    not_found = (covered.get("lines") or {}).get("notFound") or []
    if not_found:  # visible, not blocking: a person sees which lines the statement did not give
        warnings.append({"code": "LINES_NOT_FOUND", "message": f"{len(not_found)} statement line(s) not found: "
                                                               + ", ".join(not_found[:40])})
    to_review = sum(1 for f in fields if f["status"] == "NEEDS_REVIEW")
    if job.status == "NEEDS_REVIEW" or to_review:
        route, reasons = "NEEDS_REVIEW", [f"{to_review} field(s) need review"] if to_review else ["held for review"]
    else:
        route, reasons = "AUTO_ACCEPTED", []
    return {
        "schema": SCHEMA,
        "runId": job.id,
        "jobId": job.id,
        "jobType": job.job_type,
        "status": "NEEDS_REVIEW" if job.status == "NEEDS_REVIEW" else job.status,
        "documentId": job.document_id,
        "documentVersionId": job.document_version_id,
        "sha256": job.sha256 or (doc.sha256 if doc else None),
        "pipeline": {"version": pipeline_version, "skillPack": job.flow, "models": None,
                     "taxonomyVersion": taxonomy().version},
        "classification": None,
        "identifiers": [],
        "fields": fields,
        "document": {"confidence": None, "route": route, "reasons": reasons, **covered},
        "warnings": warnings,
        "rollout": job.rollout,
        "timingMs": None,
        "usage": None,
    }
