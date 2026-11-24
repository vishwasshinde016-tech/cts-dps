"""Review, reclassify, quality and the bank app's AI status contract (Phase 5).

- ``GET /dps/v1/jobs/{id}/review``: the fields a person must check, with each reader's value, the top reasons and
  the evidence (page, quote, box, page image URL). ``revision`` is the run number; send it back as If-Match.
- ``POST /dps/v1/jobs/{id}/review``: CONFIRM, CORRECT (method MANUAL), REJECT, ADD. 409 when the revision moved on
  (two reviewers at once). Decisions are kept in ``dps_review_decision``; the run is saved again as a new revision
  (old runs are never changed); dependent checks are re-derived; corrections queue golden-set candidates; when
  nothing is left to review the job becomes DONE and ``dps.extraction.completed`` is emitted.
- ``POST /dps/v1/jobs/{id}/reclassify``: a new job with the right pack; the old run is marked superseded.
- ``GET /dps/v1/ops/quality``: the measured error rate of the 3% QA sample, and the candidate queue.
- ``POST /api/v1/ai/covenant-status``: the bank app's AiStatusRecommender contract, answered from certificates DPS
  already read (never parses a file inside the call); an empty list means "AI unavailable".
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, true

from dps import feed, jobs, results
from dps.api.access import tenant
from dps.api.auth import ADMIN, SERVICE, SUPPORT, require_roles
from dps.api.deps import DB, User, job_view
from dps.errors import ApiError, not_found
from dps.models import Document, DocumentCpLink, GoldenCandidate, Job, ReviewDecision, Run, utcnow

router = APIRouter()
TYPE_TO_JOB = {"SANCTION_LETTER": "EXTRACT_SANCTION", "LOAN_AGREEMENT": "EXTRACT_AGREEMENT",
               "AMENDMENT": "EXTRACT_AMENDMENT", "FINANCIAL_STATEMENT": "EXTRACT_STATEMENT",
               "COMPLIANCE_CERTIFICATE": "EXTRACT_CERTIFICATE", "WAIVER_CONSENT": "EXTRACT_WAIVER",
               "GUARANTEE": "EXTRACT_AGREEMENT", "AMENDED_AND_RESTATED": "EXTRACT_AGREEMENT",
               "TRUST_DEED": "EXTRACT_AGREEMENT"}


def _job(db, job_id: str) -> Job:
    from dps.api.access import same_tenant

    job = db.get(Job, job_id)
    if job is None or not same_tenant(job.tenant_id):  # G2
        raise not_found("Job")
    return job


def _current(db, job: Job, request: Request) -> tuple[dict, int]:
    """The latest result and its revision (run number; 1 for a proven-flow result never reviewed here)."""
    run = db.scalars(select(Run).where(Run.job_id == job.id).order_by(Run.run_no.desc())).first()
    if run is not None:
        return json.loads(run.result_json), run.run_no
    from dps.ingest.store import LocalStore

    s = request.app.state.settings
    return results.build(db, job, s.pipeline_version, LocalStore(s.storage_dir, s.staging_dir, s.derived_dir)), 1


def is_statement(job: Job, result: dict) -> bool:
    """A statement's result: EXTRACT_STATEMENT, or AUTO when the document read as a statement (CTS integration)."""
    if job.job_type == "EXTRACT_STATEMENT":
        return True
    return job.job_type == "AUTO" and (result.get("classification") or {}).get("type") == "FINANCIAL_STATEMENT"


def _page_images(db, job: Job) -> bool:
    """Page images are drawn from PDF pages only (CTS review, 23 Nov 2026: links for DOCX, XLSX, CSV and photos
    answered 404)."""
    doc = db.get(Document, job.document_id)
    return bool(doc is not None and (doc.file_name or "").lower().endswith(".pdf"))


def _readers(f: dict) -> dict | None:
    """Each reader's reading when they differ: the rules' value, and the agent's as an object (its note), so both are
    values as review.json says (CTS review, 23 Nov 2026: ai was a bare message string)."""
    out = {}
    for i in f.get("issues", []):
        if i.get("code") == "MODELS_DISAGREE" and i.get("message"):
            out["ai"] = {"note": i["message"]}
    if out:
        out["rules"] = f["value"]
    return out or None


def acting_user(x_user: str | None, user) -> str:
    """The person a SERVICE call acts for (X-User), trimmed; the caller's own name when absent. Longer than 255
    characters is refused (it would not fit the audit columns)."""
    name = (x_user or "").strip()
    if len(name) > 255:
        raise ApiError(400, "INVALID_INPUT", "X-User is longer than 255 characters.")
    return name or user.user


def _open_warnings(result: dict) -> list[dict]:
    from dps.skills.runner import BLOCKING

    return [{"fieldId": f"warning:{w['code']}", "code": w["code"], "message": w.get("message")}
            for w in result.get("warnings", []) if w.get("code") in BLOCKING and not w.get("acknowledged")]


def _save_revision(db, job: Job, result: dict, revision: int, trigger: str) -> None:
    """Stores the result as the next revision (run). Two reviewers saving the same revision at once: the second gets
    409 REVISION_CHANGED, never a server error (the run number is unique per job)."""
    from sqlalchemy.exc import IntegrityError

    new_run = Run(job_id=job.id, run_no=revision + 1, status=result["status"], trigger=trigger, result_json="{}")
    result["runId"] = f"{job.id}:{revision + 1}"
    results.flag_format(result)
    new_run.result_json = json.dumps(result, default=str, separators=(",", ":"))
    db.add(new_run)
    try:
        db.flush()
    except IntegrityError as ex:
        db.rollback()
        raise ApiError(409, "REVISION_CHANGED", "Someone else saved this result at the same time; reload it.",
                       {"revision": revision + 1}) from ex
    prev = db.scalars(select(Run).where(Run.job_id == job.id, Run.run_no == revision)).first()
    if prev is not None:
        prev.superseded_by = result["runId"]


@router.get("/dps/v1/jobs/{job_id}/review")
def get_review(job_id: str, request: Request, db: DB, user: User) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job(db, job_id)
    if job.status not in (jobs.DONE, jobs.NEEDS_REVIEW):
        raise ApiError(404, "RESULT_NOT_READY", f"The job is {job.status}.", {"status": job.status})
    result, revision = _current(db, job, request)
    version = job.document_version_id if _page_images(db, job) else None
    fields = []
    for f in result.get("fields", []):
        if f.get("status") != "NEEDS_REVIEW":
            continue
        src = f.get("source") or {}
        reasons = list((f.get("signals") or {}).get("reasons") or []) + [i["code"] for i in f.get("issues", [])]
        fields.append({
            "fieldId": f["id"], "group": f["group"], "name": f["name"], "proposed": f["value"],
            "readers": _readers(f) or {}, "confidence": f.get("confidence"),
            "reasons": list(dict.fromkeys(reasons)),
            "evidence": {"page": src.get("page"), "quote": src.get("quote"), "bbox": src.get("bbox"),
                         "cellRef": src.get("cellRef"),
                         "pageImage": (f"/dps/v1/versions/{version}/pages/{src['page']}/image"
                                       if version and src.get("page") else None)}})
    warnings = _open_warnings(result)  # blocking warnings still open: ACKNOWLEDGE them (fieldId warning:<CODE>)
    return {"jobId": job.id, "runId": result.get("runId", job.id), "revision": revision, "fields": fields,
            "warnings": warnings,
            "counts": {"toReview": len(fields), "total": len(result.get("fields", [])), "openWarnings": len(warnings)}}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fieldId: str = Field(min_length=1, max_length=64)  # noqa: N815
    # ACKNOWLEDGE: fieldId is "warning:<CODE>" and comment says why the warning does not block (full review G29)
    action: Literal["CONFIRM", "CORRECT", "REJECT", "ADD", "ACKNOWLEDGE"]
    group: str | None = None
    name: str | None = Field(default=None, max_length=255)
    value: object | None = None
    page: int | None = Field(default=None, ge=1)
    reasonCode: Literal["WRONG_VALUE", "WRONG_UNIT", "WRONG_PAGE", "NOT_A_COVENANT", "MISSED",  # noqa: N815
                        "OTHER"] | None = None
    comment: str | None = Field(default=None, max_length=2000)


class Decisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[Decision] = Field(min_length=1, max_length=500)


_PLAIN_FORMULA = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


def _consistent_covenant(f: dict, change, reviewer: str) -> None:
    """A corrected covenant stays consistent: a corrected formula sets the operator and threshold it states; a
    corrected operator or threshold rebuilds the formula (on the measure the app uses). The person's formula is what
    the app validated in its own editor, so it is mappable (CTS review, 23 Nov 2026)."""
    from dps.skills import app_formula
    from dps.skills.covenants.codes import COVENANT_GROUPS

    value = f.get("value")
    if f.get("group") not in COVENANT_GROUPS or not isinstance(value, dict) or not isinstance(change, dict):
        return
    mapping = value.get("mapping") if isinstance(value.get("mapping"), dict) else {}
    expression = change.get("expression")
    if isinstance(expression, str) and expression.strip():
        m = _PLAIN_FORMULA.match(expression)
        if m:
            value["operator"] = "=" if m.group(2) == "==" else m.group(2)
            value["threshold"] = float(m.group(3))
        value["expression"] = expression.strip()
        value["mapping"] = {"mappable": True, "reasons": [], "notes": [f"CORRECTED: formula set by {reviewer}"[:200]]
                            + ([] if m else ["CUSTOM: not a single comparison; the app validated it"]),
                            "measure": m.group(1) if m else mapping.get("measure"), "unit": None}
        return
    if any(k in change for k in ("operator", "threshold", "unit", "metric", "schedule")):
        measure = mapping.get("measure")
        app_formula.build(f, {"metricKeys": [measure]} if measure else None)
        value["mapping"]["notes"].append(f"CORRECTED: by {reviewer}"[:200])


PERIOD_FIELD = "document:asOfDate"


def _set_period(result: dict, d: Decision, reviewer: str) -> tuple[dict, dict]:
    """A statement's period end set by a person (improvement plan N4, 24 Nov 2026): CORRECT on fieldId
    document:asOfDate with value {"asOfDate": "YYYY-MM-DD"}. Every statement line takes the date, and a
    PERIOD_DATE_MISSING warning is acknowledged by it. Before, only a reclassify could change the date."""
    if d.action != "CORRECT" or not isinstance(d.value, dict):
        raise ApiError(400, "VALIDATION_FAILED", f"{PERIOD_FIELD} takes CORRECT with value {{\"asOfDate\": ...}}.")
    try:
        day = date.fromisoformat(str(d.value.get("asOfDate")))
    except ValueError as ex:
        raise ApiError(400, "VALIDATION_FAILED", "asOfDate must be a date (YYYY-MM-DD).") from ex
    lines = [f for f in result.get("fields", []) if f.get("group") == "STATEMENT_LINE"]
    before = next((f["value"].get("asOfDate") for f in lines if isinstance(f.get("value"), dict)), None)
    for f in lines:
        if isinstance(f.get("value"), dict):
            f["value"]["asOfDate"] = day.isoformat()
    for w in result.get("warnings", []):
        if w.get("code") in ("PERIOD_DATE_MISSING", "PERIOD_FROM_DOCUMENT") and not w.get("acknowledged"):
            w["acknowledged"] = {"by": reviewer, "comment": f"period end set to {day.isoformat()}"}
    result["_periodSet"] = day.isoformat()  # stored on the document by submit_review
    if not d.reasonCode:
        d.reasonCode = "WRONG_VALUE"
    return {"value": {"asOfDate": before}}, {"value": {"asOfDate": day.isoformat()}}


def _apply(result: dict, d: Decision, reviewer: str) -> tuple[dict | None, dict | None]:
    """Applies one decision to the result in place; returns (old field, new field)."""
    if d.fieldId == PERIOD_FIELD:
        return _set_period(result, d, reviewer)
    if d.action == "ACKNOWLEDGE":
        code = d.fieldId.removeprefix("warning:")
        warning = next((w for w in result.get("warnings", []) if w.get("code") == code), None)
        if warning is None or not d.fieldId.startswith("warning:"):
            raise ApiError(400, "VALIDATION_FAILED", "ACKNOWLEDGE needs fieldId warning:<CODE> of a warning in this "
                                                     "result.")
        if not d.comment:
            raise ApiError(400, "VALIDATION_FAILED", "ACKNOWLEDGE needs a comment saying why it does not block.")
        old = json.loads(json.dumps(warning))
        warning["acknowledged"] = {"by": reviewer, "comment": d.comment}
        return old, warning
    by_id = {f["id"]: f for f in result.get("fields", [])}
    if d.action == "ADD":
        if not (d.group and d.name and d.page and d.value is not None and d.reasonCode):
            raise ApiError(400, "VALIDATION_FAILED", "ADD needs group, name, value, page and reasonCode.")
        if d.fieldId in by_id:
            raise ApiError(400, "VALIDATION_FAILED", f"field {d.fieldId} already exists.")
        new = {"id": d.fieldId, "group": d.group, "name": d.name, "value": d.value,
               "source": {"page": d.page, "quote": None, "bbox": None, "blockId": None, "cellRef": None},
               "test": None, "libraryMatch": None, "method": "MANUAL", "confidence": 1.0,
               "signals": {"reviewer": reviewer}, "issues": [], "status": "CONFIRMED"}
        result["fields"].append(new)
        return None, new
    f = by_id.get(d.fieldId)
    if f is None:
        raise ApiError(400, "VALIDATION_FAILED", f"field {d.fieldId} is not in this result.")
    old = json.loads(json.dumps(f))
    if d.action == "CONFIRM":
        f["status"] = "CONFIRMED"
    elif d.action == "REJECT":
        f["status"] = "REJECTED"
    else:  # CORRECT
        if d.value is None:
            raise ApiError(400, "VALIDATION_FAILED", "CORRECT needs value.")
        if not d.reasonCode:
            d.reasonCode = "WRONG_VALUE"  # the usual reason; the caller may send another (CTS review, 23 Nov 2026)
        f["value"] = {**f["value"], **d.value} if isinstance(f["value"], dict) and isinstance(d.value, dict) \
            else d.value
        f["method"], f["status"], f["confidence"] = "MANUAL", "CORRECTED", 1.0
        _consistent_covenant(f, d.value, reviewer)
    f["signals"] = {**(f.get("signals") or {}), "reviewer": reviewer}
    return old, f


@router.post("/dps/v1/jobs/{job_id}/review", status_code=202)
def submit_review(job_id: str, body: Decisions, request: Request, db: DB, user: User,
                  if_match: Annotated[str | None, Header(alias="If-Match")] = None,
                  x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job(db, job_id)
    if job.status == jobs.DONE:
        # A finished review is not reopened: a second "completed" event would make Spring apply it twice (CTS review)
        raise ApiError(409, "JOB_FINISHED", "The review of this job is finished.")
    if job.status != jobs.NEEDS_REVIEW:
        raise ApiError(409, "JOB_NOT_RETRYABLE", f"The job is {job.status}; nothing to review.")
    result, revision = _current(db, job, request)
    if if_match is None or if_match.strip('"') != str(revision):
        raise ApiError(409, "REVISION_CHANGED", "The result changed since you loaded it; reload and review again.",
                       {"revision": revision})
    if any(d.fieldId == PERIOD_FIELD for d in body.decisions) and not is_statement(job, result):
        raise ApiError(400, "VALIDATION_FAILED", f"{PERIOD_FIELD} is only for a statement's result.")
    reviewer = acting_user(x_user, user)
    has_run = db.scalar(select(func.count()).select_from(Run).where(Run.job_id == job.id))
    if not has_run:  # a proven-flow result reviewed here for the first time: keep the original as revision 1
        db.add(Run(job_id=job.id, run_no=1, status=result["status"], trigger="SUBMIT",
                   result_json=json.dumps(result, default=str, separators=(",", ":"))))
        db.flush()
    for d in body.decisions:
        old, new = _apply(result, d, reviewer)
        sampled = bool((old or {}).get("signals", {}) and (old or {}).get("signals", {}).get("sampled"))
        db.add(ReviewDecision(job_id=job.id, run_no=revision + 1, field_id=d.fieldId, reviewer=reviewer,
                              action=d.action,
                              old_value_json=json.dumps(old.get("value", old), default=str) if old else None,
                              new_value_json=json.dumps(new.get("value", new), default=str) if new else None,
                              reason_code=d.reasonCode, comment=d.comment, sampled=sampled))
        if d.action in ("CORRECT", "ADD", "REJECT"):
            db.add(GoldenCandidate(job_id=job.id, field_id=d.fieldId, job_type=job.job_type,
                                   payload_json=json.dumps({"action": d.action, "before": old, "after": new,
                                                            "reasonCode": d.reasonCode, "comment": d.comment,
                                                            "versionId": job.document_version_id}, default=str)))
        if job.rollout != "SHADOW":
            _learn(db, job, d, old, new, reviewer)
    period = result.pop("_periodSet", None)
    if period:  # the statement's period end, as the person set it, is the document's (publish uses it)
        doc = db.get(Document, job.document_id)
        if doc is not None:
            doc.as_of_date = date.fromisoformat(period)
    _rederive(result, job)
    remaining = [f for f in result["fields"] if f["status"] == "NEEDS_REVIEW"]
    from dps.skills.runner import BLOCKING

    # Every blocking warning (mismatched entity, period or currency, unreadable pages, nothing found ...) stays open
    # until a person acknowledges it with a reason (full review G29).
    blocking = [w for w in result.get("warnings", []) if w["code"] in BLOCKING and not w.get("acknowledged")]
    result["status"] = "NEEDS_REVIEW" if remaining or blocking else "DONE"
    reasons = ([f"{len(remaining)} field(s) need review"] if remaining else []) + [w["code"] for w in blocking]
    result["document"] = {**result.get("document", {}),
                          "route": "NEEDS_REVIEW" if remaining or blocking else "REVIEWED", "reasons": reasons}
    _save_revision(db, job, result, revision, "REVIEW")  # a corrected value is checked against result.json too
    if result["status"] == "DONE" and is_statement(job, result) and job.rollout != "SHADOW":
        # The approved figures go to the values table (the bank app's /api/data/values) only now (G21).
        from dps.skills.statement_lines.reader import CurrencyNotSettled, publish

        settings = request.app.state.settings
        try:
            publish(db, job, result, settings.report_derived_values, settings.fiscal_year_end_month, reviewer)
        except CurrencyNotSettled as ex:
            db.rollback()
            raise ApiError(409, "CURRENCY_DIFFERS", str(ex)) from ex
    if result["status"] == "DONE":
        job.status, job.updated_at, job.finished_at = jobs.DONE, utcnow(), utcnow()
        job.error_code = None
        event = "dps.extraction.shadow" if job.rollout == "SHADOW" else "dps.extraction.completed"
        feed.emit_for_job(db, event, job,
                          {"resultUrl": f"/dps/v1/jobs/{job.id}/result", "revision": revision + 1,
                           "reviewedBy": reviewer})
    db.commit()
    db.refresh(job)
    return {**job_view(job), "revision": revision + 1, "remaining": len(remaining)}


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


@router.post("/dps/v1/jobs/{job_id}/close")
def close_review(job_id: str, body: CloseRequest, request: Request, db: DB, user: User,
                 if_match: Annotated[str | None, Header(alias="If-Match")] = None,
                 x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    """The caller (CTS) has finished with this result: fields it did not decide become NOT_USED (no learning, no
    golden candidate: nobody judged them), blocking warnings still open are recorded as shown to the user in CTS, and
    the job is DONE with route CLOSED (CTS integration, 23 Nov 2026: the app decides only the covenants it shows,
    so without this a job never finished). Statement figures are never published by a close. Safe to repeat."""
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job(db, job_id)
    closer = acting_user(x_user, user)
    if job.status == jobs.DONE:
        return {**job_view(job), "revision": _current(db, job, request)[1], "remaining": 0, "closed": True}
    if job.status != jobs.NEEDS_REVIEW:
        raise ApiError(409, "JOB_NOT_RETRYABLE", f"The job is {job.status}; there is no review to close.")
    result, revision = _current(db, job, request)
    if if_match is not None and if_match.strip('"') != str(revision):
        raise ApiError(409, "REVISION_CHANGED", "The result changed since you loaded it; reload and close again.",
                       {"revision": revision})
    has_run = db.scalar(select(func.count()).select_from(Run).where(Run.job_id == job.id))
    if not has_run:
        db.add(Run(job_id=job.id, run_no=1, status=result["status"], trigger="SUBMIT",
                   result_json=json.dumps(result, default=str, separators=(",", ":"))))
        db.flush()
    not_used = 0
    for f in result.get("fields", []):
        if f.get("status") == "NEEDS_REVIEW":
            f["status"], not_used = "NOT_USED", not_used + 1
    from dps.skills.runner import BLOCKING

    held = [w["code"] for w in result.get("warnings", []) if w.get("code") in BLOCKING and not w.get("acknowledged")]
    shown = 0
    for w in result.get("warnings", []):
        if w.get("code") in BLOCKING and not w.get("acknowledged"):
            comment = f"shown to the user in CTS; review closed: {body.reason}"[:500]
            w["acknowledged"] = {"by": closer, "comment": comment, "byClose": True}
            shown += 1
    result["status"] = "DONE"
    result["document"] = {**result.get("document", {}), "route": "CLOSED",
                          "reasons": [f"closed by {closer}: {body.reason}"[:300]]}
    _save_revision(db, job, result, revision, "CLOSE")
    job.status, job.updated_at, job.finished_at = jobs.DONE, utcnow(), utcnow()
    job.error_code = None
    from dps import services

    published, not_published = _publish_on_close(db, job, result, held, closer, request)
    services.audit(db, closer, "REVIEW_CLOSE", "job", job.id, notUsed=not_used, warningsShown=shown,
                   reason=body.reason[:200], published=published)
    event = "dps.extraction.shadow" if job.rollout == "SHADOW" else "dps.extraction.completed"
    payload = {"resultUrl": f"/dps/v1/jobs/{job.id}/result", "revision": revision + 1, "reviewedBy": closer,
               "closed": True, "notUsed": not_used}
    if published is not None:
        payload["published"] = published
    feed.emit_for_job(db, event, job, payload)
    db.commit()
    db.refresh(job)
    out = {**job_view(job), "revision": revision + 1, "remaining": 0, "closed": True, "notUsed": not_used}
    if published is not None:
        out["published"] = published
        if not_published:
            out["notPublishedReason"] = not_published
    return out


def _publish_on_close(db, job: Job, result: dict, held: list[str], closer: str,
                      request: Request) -> tuple[int | None, str | None]:
    """A statement closed in CTS (improvement plan N3, 24 Nov 2026): the lines a person CONFIRMED or CORRECTED are
    stored for the bank app's /api/data/values; lines nobody decided are not. Nothing is stored when a blocking
    warning was still open at the close (an unsettled currency, entity, period or total must be acknowledged by a
    person first) or in SHADOW. Returns (lines stored, or None when not a statement; the reason when none)."""
    if not is_statement(job, result):
        return None, None
    if job.rollout == "SHADOW":
        return 0, "SHADOW: figures are not stored during the pilot"
    if held:
        return 0, "blocking warning(s) open at the close: " + ", ".join(held)
    decided = [f for f in result.get("fields", [])
               if f.get("group") != "STATEMENT_LINE" or f.get("status") in ("CONFIRMED", "CORRECTED")]
    if not any(f.get("group") == "STATEMENT_LINE" for f in decided):
        return 0, "no statement line was confirmed or corrected"
    from dps.skills.statement_lines.reader import CurrencyNotSettled, publish

    settings = request.app.state.settings
    try:
        stored = publish(db, job, {**result, "fields": decided}, settings.report_derived_values,
                         settings.fiscal_year_end_month, closer)
    except CurrencyNotSettled as ex:
        return 0, str(ex)[:300]
    return stored, None if stored else "the statement has no period date"


def _rederive(result: dict, job: Job) -> None:
    """Dependent checks after corrections: statement identities run again on the corrected figures."""
    if is_statement(job, result):
        from dps.skills import runner as skills

        kept = [w for w in result.get("warnings", []) if w["code"] != "TOTALS_DO_NOT_ADD"]
        live = [f for f in result["fields"] if f["status"] != "REJECTED"]
        result["warnings"] = kept + skills.statement_warnings(live)


def _learn(db, job: Job, d: Decision, old: dict | None, new: dict | None, reviewer: str) -> None:
    """A confirmed or corrected statement line teaches label memory for that borrower (used by the statement
    reader next time). Prompts and rules are never changed automatically."""
    if d.action not in ("CONFIRM", "CORRECT") or new is None or new.get("group") != "STATEMENT_LINE":
        return
    quote = " ".join(((new.get("source") or {}).get("quote") or "").split())
    if not quote:
        return
    from dps import services

    for cp_id in db.scalars(select(DocumentCpLink.cp_id).where(DocumentCpLink.document_id == job.document_id)):
        services.remember_label(db, cp_id, quote, new["name"][:64], job.document_id, reviewer)


class ReclassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["SANCTION_LETTER", "LOAN_AGREEMENT", "AMENDMENT", "FINANCIAL_STATEMENT", "COMPLIANCE_CERTIFICATE",
                  "SECURITY_DOCUMENT", "OTHER_EVIDENCE", "WAIVER_CONSENT", "GUARANTEE", "AMENDED_AND_RESTATED",
                  "TRUST_DEED"]
    reason: str = Field(min_length=1, max_length=500)
    # A statement's period end: kept from the first request when not sent; sent when the statement names none
    # (PERIOD_DATE_MISSING) or was read wrong (CTS integration, 23 Nov 2026).
    asOfDate: date | None = None  # noqa: N815 - contract field name


@router.post("/dps/v1/jobs/{job_id}/reclassify", status_code=202)
def reclassify(job_id: str, body: ReclassifyRequest, request: Request, response: Response, db: DB, user: User,
               x_user: Annotated[str | None, Header(alias="X-User")] = None,
               idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    from dps import services
    from dps.api.jobs import JOB_TYPES

    old = _job(db, job_id)
    actor = acting_user(x_user, user)
    new_type = TYPE_TO_JOB.get(body.type)
    if new_type is None:
        raise ApiError(422, "UNSUPPORTED_JOB_TYPE", f"No extraction pack reads {body.type} documents.")
    if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key has 8 to 128 characters.")
    if idempotency_key:  # a retried request returns the job it made (CTS review, 23 Nov 2026: it made two)
        seen = db.scalars(select(Job).where(Job.tenant_id == old.tenant_id,
                                            Job.idempotency_key == idempotency_key)).first()
        if seen is not None:
            response.status_code = 200
            return {"jobId": seen.id, "status": seen.status, "duplicate": True,
                    "links": {"self": f"/dps/v1/jobs/{seen.id}", "result": f"/dps/v1/jobs/{seen.id}/result"}}
    request_body = json.loads(old.request_json or "{}")
    request_body["jobType"] = new_type
    ctx = request_body.setdefault("context", {})  # asOfDate and the rest of the caller's context are kept
    ctx["expectedType"], ctx["reprocessReason"] = body.type, body.reason[:500]
    doc = db.get(Document, old.document_id)
    if body.asOfDate is not None:
        ctx["asOfDate"] = body.asOfDate.isoformat()
        if doc is not None:
            doc.as_of_date = body.asOfDate
    elif doc is not None and doc.as_of_date is not None and not ctx.get("asOfDate"):
        ctx["asOfDate"] = doc.as_of_date.isoformat()  # the period the first reading found stays with the document
    spec = JOB_TYPES[new_type]
    settings = request.app.state.settings
    new = Job(document_id=old.document_id, flow=spec["flow"], job_type=new_type, tenant_id=old.tenant_id,
              priority=jobs.PRIORITY.get("INTERACTIVE", 1), due_at=utcnow(),
              request_json=json.dumps(request_body), correlation_id=old.correlation_id,
              pipeline_version=settings.pipeline_version, document_version_id=old.document_version_id,
              sha256=old.sha256, requested_by=actor, idempotency_key=idempotency_key,
              rollout=old.rollout or "NORMAL")
    db.add(new)
    db.flush()
    _supersede(db, old, new, actor, request)
    feed.emit_for_job(db, "dps.job.accepted", new, {"jobType": new_type, "priority": "INTERACTIVE",
                                                   "reclassifiedFrom": old.id})
    services.audit(db, actor, "JOB_RECLASSIFY", "job", old.id, newJob=new.id, type=body.type,
                   reason=body.reason[:500])
    db.commit()
    response.headers["Location"] = f"/dps/v1/jobs/{new.id}"
    return {"jobId": new.id, "status": new.status, "duplicate": False,
            "links": {"self": f"/dps/v1/jobs/{new.id}", "result": f"/dps/v1/jobs/{new.id}/result"}}


def _supersede(db, old: Job, new: Job, actor: str, request: Request) -> None:
    """The job read again as another type ends (CTS review, 23 Nov 2026: it stayed open and could still complete):
    waiting or running, it is cancelled; waiting for review, its open fields become NOT_USED and it is DONE with
    route SUPERSEDED. Either way ``dps.job.superseded`` names the new job."""
    if old.status in (jobs.QUEUED, jobs.RETRY_WAIT, jobs.RUNNING):
        try:
            jobs.request_cancel(db, old, actor)
        except jobs.JobFinished:
            pass
    elif old.status == jobs.NEEDS_REVIEW:
        result, revision = _current(db, old, request)
        for f in result.get("fields", []):
            if f.get("status") == "NEEDS_REVIEW":
                f["status"] = "NOT_USED"
        result["status"] = "DONE"
        result["document"] = {**result.get("document", {}), "route": "SUPERSEDED",
                              "reasons": [f"read again as another type: job {new.id}"]}
        if not db.scalar(select(func.count()).select_from(Run).where(Run.job_id == old.id)):
            db.add(Run(job_id=old.id, run_no=1, status="NEEDS_REVIEW", trigger="SUBMIT",
                       result_json=json.dumps(result, default=str, separators=(",", ":"))))
            db.flush()
        _save_revision(db, old, result, revision, "SUPERSEDED")
        old.status, old.updated_at, old.finished_at = jobs.DONE, utcnow(), utcnow()
    db.flush()
    for run in db.scalars(select(Run).where(Run.job_id == old.id)):  # every reading of the old job: the new job
        run.superseded_by = new.id
    feed.emit_for_job(db, "dps.job.superseded", old, {"supersededBy": new.id, "status": old.status})


@router.get("/dps/v1/ops/quality")
def quality(db: DB, user: User) -> dict:
    """The 3% QA sample's measured error rate, per job type, and the golden-set candidate queue."""
    require_roles(user, ADMIN, SUPPORT)
    rows = db.execute(select(Job.job_type, ReviewDecision.action, func.count())
                      .join(Job, Job.id == ReviewDecision.job_id)
                      .where(ReviewDecision.sampled == true())
                      .group_by(Job.job_type, ReviewDecision.action)).all()
    per: dict[str, dict] = {}
    for job_type, action, n in rows:
        row = per.setdefault(job_type, {"jobType": job_type, "sampled": 0, "wrong": 0})
        row["sampled"] += n
        if action in ("CORRECT", "REJECT"):
            row["wrong"] += n
    for row in per.values():
        row["errorRate"] = round(row["wrong"] / row["sampled"], 4) if row["sampled"] else None
    pending = db.scalar(select(func.count()).select_from(GoldenCandidate)
                        .where(GoldenCandidate.status == "PENDING")) or 0
    return {"sampleRate": 0.03, "byJobType": sorted(per.values(), key=lambda r: r["jobType"]),
            "goldenCandidatesPending": pending}


@router.get("/dps/v1/ops/go-live")
def go_live(request: Request, db: DB, user: User, days: int = 30) -> dict:
    """Phase 7: measured numbers per job type against the go-live targets, and the rollout mode in force."""
    require_roles(user, ADMIN, SUPPORT)
    from dps.ops import golive

    settings = request.app.state.settings
    body = golive.report(db, max(1, min(days, 366)))
    for t in body["types"]:
        t["rollout"] = settings.rollout_modes.get(t["jobType"], settings.rollout_default)
    return body


@router.get("/dps/v1/ops/golden-candidates")
def golden_candidates(db: DB, user: User, status: str = "PENDING") -> dict:
    require_roles(user, ADMIN, SUPPORT)
    items = db.scalars(select(GoldenCandidate).where(GoldenCandidate.status == status)
                       .order_by(GoldenCandidate.created_at).limit(200)).all()
    return {"items": [{"id": c.id, "jobId": c.job_id, "fieldId": c.field_id, "jobType": c.job_type,
                       "status": c.status, "payload": json.loads(c.payload_json),
                       "createdAt": c.created_at.isoformat()} for c in items]}


@router.post("/dps/v1/ops/golden-candidates/{candidate_id}/{decision}")
def decide_candidate(candidate_id: str, decision: Literal["accept", "reject"], db: DB, user: User,
                     x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    require_roles(user, ADMIN, SUPPORT)
    c = db.get(GoldenCandidate, candidate_id)
    if c is None:
        raise not_found("Candidate")
    if c.status != "PENDING":
        raise ApiError(409, "JOB_FINISHED", f"The candidate is already {c.status}.")
    c.status, c.decided_by, c.decided_at = ("ACCEPTED" if decision == "accept" else "REJECTED"), \
        (x_user or user.user), utcnow()
    db.commit()
    return {"id": c.id, "status": c.status}


# ---------- the bank app's AiStatusRecommender contract ----------

class CovenantIn(BaseModel):
    appliedCovenantId: str  # noqa: N815
    covenantName: str | None = None  # noqa: N815
    covenantDescription: str | None = None  # noqa: N815
    conditionExpression: str | None = None  # noqa: N815
    computedValue: str | None = None  # noqa: N815
    latestResult: str | None = None  # noqa: N815
    attachmentFileIds: list[str] = Field(default_factory=list)  # noqa: N815


class CovenantStatusRequest(BaseModel):
    cpId: str  # noqa: N815
    certificateFileIds: list[str] = Field(  # noqa: N815
        default_factory=list, description="DMS version IDs submitted as document.versionId for extraction.")
    includeCertificateDiagnostics: bool = False  # noqa: N815
    covenants: list[CovenantIn]


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


_LEADING_MEASURE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:>=|<=|==|!=|>|<)")


def _covenant_keys(*names: str | None) -> set[str]:
    """Every name a covenant goes by: its taxonomy code, its metric and its plain name, so "DSCR", "Minimum DSCR"
    and "Debt Service Coverage Ratio" meet, and the bank app's measure codes (INTEREST_COVER, DEBT_EBITDA ...) meet
    the certificate's wording (CTS review, 23 Nov 2026: only exact names matched)."""
    from dps.extraction.agreement import _best_metric
    from dps.skills.covenants.taxonomy import get as taxonomy

    tax = taxonomy()
    keys: set[str] = set()
    for name in names:
        if not name:
            continue
        keys |= {k for k in (tax.code_for(name), _best_metric(name), _norm(name)) if k}
    return keys


@router.post("/api/v1/ai/covenant-status")
def covenant_status(body: CovenantStatusRequest, db: DB, user: User) -> dict:
    """Recommendations from compliance certificates DPS already extracted for the CP (rationale cites the page).

    Only figures a person confirmed or corrected count (Phase 3 fix 2, 23 Nov 2026): a job still waiting for review,
    a SHADOW job, and a field no reviewer confirmed never become a COMPLIED or BREACHED recommendation.

    Which certificate (CTS integration, 23 Nov 2026): the ones named in certificateFileIds (DMS version ids), else
    the newest certificate only, so a figure from an older period is never offered for this one. Certificates read
    by AUTO count like EXTRACT_CERTIFICATE. A covenant is matched by its code, metric and name; two figures that
    match one covenant and disagree give no recommendation."""
    require_roles(user, SERVICE, ADMIN)
    wanted = list(dict.fromkeys(body.certificateFileIds))
    diagnostics = dict.fromkeys(wanted, "UNAVAILABLE") if body.includeCertificateDiagnostics else {}
    statuses = (jobs.DONE, jobs.NEEDS_REVIEW) if diagnostics else (jobs.DONE,)
    doc_ids = select(DocumentCpLink.document_id).where(DocumentCpLink.cp_id == body.cpId)
    query = select(Job).where(Job.document_id.in_(doc_ids),
                              Job.job_type.in_(("EXTRACT_CERTIFICATE", "AUTO")),
                              Job.status.in_(statuses), Job.rollout != "SHADOW")
    if scope := tenant():
        query = query.where(Job.tenant_id == scope)
    cert_jobs = db.scalars(query.order_by(Job.finished_at.desc(), Job.created_at.desc())).all()
    selected = {version for version in wanted if version.strip()}
    figures: list[tuple[set[str], dict, str]] = []
    for job in cert_jobs:
        version = job.document_version_id
        if wanted and version not in selected:
            continue
        run = db.scalars(select(Run).where(Run.job_id == job.id).order_by(Run.run_no.desc())).first()
        result = json.loads(run.result_json) if run is not None else {}
        certified = [f for f in result.get("fields", []) if f.get("group") == "CERTIFIED_FIGURE"]
        if diagnostics.get(version) == "UNAVAILABLE" and (
                certified or job.job_type == "EXTRACT_CERTIFICATE"
                or (result.get("classification") or {}).get("type") == "COMPLIANCE_CERTIFICATE"):
            if job.status == jobs.NEEDS_REVIEW:
                diagnostics[version] = "AWAITING_REVIEW"
            elif run is not None:
                diagnostics[version] = "NO_REVIEWED_FIGURES"
        if job.status != jobs.DONE:
            continue
        if not certified:
            continue  # an AUTO job that read another type of document
        for f in certified:
            if f.get("status") in ("CONFIRMED", "CORRECTED"):
                keys = _covenant_keys(f["value"].get("covenant"))
                figures.append((keys, f, version or job.id))
                if version in diagnostics and keys and f["value"].get("declaredCompliant") is not None:
                    diagnostics[version] = "REVIEWED_FIGURES_AVAILABLE"
        if not wanted:
            break  # the newest certificate only
    out = []
    for c in body.covenants:
        measure = _LEADING_MEASURE.match(c.conditionExpression or "")
        keys = _covenant_keys(measure.group(1) if measure else None, c.covenantName)
        hits = [(f, version) for fkeys, f, version in figures if fkeys & keys]
        if not hits or len({f["value"].get("declaredCompliant") for f, _ in hits}) > 1:
            continue  # nothing certified for it, or two certified figures that disagree
        f, version = hits[0]
        declared = f["value"].get("declaredCompliant")
        if declared is None:
            continue
        status = "COMPLIED" if declared else "BREACHED"
        page = (f.get("source") or {}).get("page")
        rationale = (f"Compliance certificate (version {version}, page {page}) certifies "
                     f"{f['value'].get('covenant')} at {f['value'].get('figure')} and declares it "
                     f"{'complied' if declared else 'not complied'}. Advisory only; the analyst decides.")
        confidence = f.get("confidence")
        out.append({"appliedCovenantId": c.appliedCovenantId, "recommendedStatus": status,
                    "rationale": rationale[:1000], "confidence": confidence})
    response = {"recommendations": out}
    if body.includeCertificateDiagnostics:
        response["certificateDiagnostics"] = [{"versionId": version, "status": status}
                                               for version, status in diagnostics.items()]
    return response
