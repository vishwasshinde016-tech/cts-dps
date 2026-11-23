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
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, true

from dps import feed, jobs, results
from dps.api.auth import ADMIN, SERVICE, SUPPORT, require_roles
from dps.api.deps import DB, User, job_view
from dps.errors import ApiError, not_found
from dps.models import DocumentCpLink, GoldenCandidate, Job, ReviewDecision, Run, utcnow

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


def _readers(f: dict) -> dict | None:
    out = {}
    for i in f.get("issues", []):
        if i.get("code") == "MODELS_DISAGREE" and i.get("message"):
            out["ai"] = i["message"]
    if out:
        out["rules"] = f["value"]
    return out or None


@router.get("/dps/v1/jobs/{job_id}/review")
def get_review(job_id: str, request: Request, db: DB, user: User) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job(db, job_id)
    if job.status not in (jobs.DONE, jobs.NEEDS_REVIEW):
        raise ApiError(404, "RESULT_NOT_READY", f"The job is {job.status}.", {"status": job.status})
    result, revision = _current(db, job, request)
    version = job.document_version_id
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
    return {"jobId": job.id, "runId": result.get("runId", job.id), "revision": revision, "fields": fields,
            "counts": {"toReview": len(fields), "total": len(result.get("fields", []))}}


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


def _apply(result: dict, d: Decision, reviewer: str) -> tuple[dict | None, dict | None]:
    """Applies one decision to the result in place; returns (old field, new field)."""
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
        if d.value is None or not d.reasonCode:
            raise ApiError(400, "VALIDATION_FAILED", "CORRECT needs value and reasonCode.")
        f["value"] = {**f["value"], **d.value} if isinstance(f["value"], dict) and isinstance(d.value, dict) \
            else d.value
        f["method"], f["status"], f["confidence"] = "MANUAL", "CORRECTED", 1.0
    f["signals"] = {**(f.get("signals") or {}), "reviewer": reviewer}
    return old, f


@router.post("/dps/v1/jobs/{job_id}/review", status_code=202)
def submit_review(job_id: str, body: Decisions, request: Request, db: DB, user: User,
                  if_match: Annotated[str | None, Header(alias="If-Match")] = None,
                  x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job(db, job_id)
    if job.status not in (jobs.DONE, jobs.NEEDS_REVIEW):
        raise ApiError(409, "JOB_NOT_RETRYABLE", f"The job is {job.status}; nothing to review.")
    result, revision = _current(db, job, request)
    if if_match is None or if_match.strip('"') != str(revision):
        raise ApiError(409, "REVISION_CHANGED", "The result changed since you loaded it; reload and review again.",
                       {"revision": revision})
    reviewer = x_user or user.user
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
    new_run = Run(job_id=job.id, run_no=revision + 1, status=result["status"], trigger="REVIEW",
                  result_json="{}")
    result["runId"] = f"{job.id}:{revision + 1}"
    from dps import results

    results.flag_format(result)  # a corrected value is checked against result.json too
    new_run.result_json = json.dumps(result, default=str, separators=(",", ":"))
    db.add(new_run)
    prev = db.scalars(select(Run).where(Run.job_id == job.id, Run.run_no == revision)).first()
    if prev is not None:
        prev.superseded_by = result["runId"]
    if result["status"] == "DONE" and job.job_type == "EXTRACT_STATEMENT" and job.rollout != "SHADOW":
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


def _rederive(result: dict, job: Job) -> None:
    """Dependent checks after corrections: statement identities run again on the corrected figures."""
    if job.job_type == "EXTRACT_STATEMENT":
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


@router.post("/dps/v1/jobs/{job_id}/reclassify", status_code=202)
def reclassify(job_id: str, body: ReclassifyRequest, request: Request, response: Response, db: DB, user: User,
               x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    from dps import services
    from dps.api.jobs import JOB_TYPES

    old = _job(db, job_id)
    new_type = TYPE_TO_JOB.get(body.type)
    if new_type is None:
        raise ApiError(422, "UNSUPPORTED_JOB_TYPE", f"No extraction pack reads {body.type} documents.")
    request_body = json.loads(old.request_json or "{}")
    request_body["jobType"] = new_type
    ctx = request_body.setdefault("context", {})
    ctx["expectedType"], ctx["reprocessReason"] = body.type, body.reason[:500]
    spec = JOB_TYPES[new_type]
    settings = request.app.state.settings
    new = Job(document_id=old.document_id, flow=spec["flow"], job_type=new_type, tenant_id=old.tenant_id,
              priority=jobs.PRIORITY.get("INTERACTIVE", 1), due_at=utcnow(),
              request_json=json.dumps(request_body), correlation_id=old.correlation_id,
              pipeline_version=settings.pipeline_version, document_version_id=old.document_version_id,
              sha256=old.sha256, requested_by=x_user or user.user)
    db.add(new)
    db.flush()
    for run in db.scalars(select(Run).where(Run.job_id == old.id, Run.superseded_by.is_(None))):
        run.superseded_by = new.id
    feed.emit_for_job(db, "dps.job.accepted", new, {"jobType": new_type, "priority": "INTERACTIVE",
                                                   "reclassifiedFrom": old.id})
    services.audit(db, x_user or user.user, "JOB_RECLASSIFY", "job", old.id, newJob=new.id, type=body.type,
                   reason=body.reason[:500])
    db.commit()
    response.headers["Location"] = f"/dps/v1/jobs/{new.id}"
    return {"jobId": new.id, "status": new.status, "duplicate": False,
            "links": {"self": f"/dps/v1/jobs/{new.id}", "result": f"/dps/v1/jobs/{new.id}/result"}}


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
    certificateFileIds: list[str] = Field(default_factory=list)  # noqa: N815
    covenants: list[CovenantIn]


def _norm(text: str | None) -> str:
    import re

    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@router.post("/api/v1/ai/covenant-status")
def covenant_status(body: CovenantStatusRequest, db: DB, user: User) -> dict:
    """Recommendations from compliance certificates DPS already extracted for the CP (rationale cites the page)."""
    require_roles(user, SERVICE, ADMIN)
    from dps.extraction.agreement import _best_metric

    doc_ids = [d for (d,) in db.execute(select(DocumentCpLink.document_id).where(DocumentCpLink.cp_id == body.cpId))]
    if not doc_ids:
        return {"recommendations": []}
    cert_jobs = db.scalars(select(Job).where(Job.document_id.in_(doc_ids), Job.job_type == "EXTRACT_CERTIFICATE",
                                             Job.status.in_((jobs.DONE, jobs.NEEDS_REVIEW)))
                           .order_by(Job.finished_at.desc())).all()
    figures: dict[str, tuple[dict, str]] = {}
    for job in cert_jobs:
        run = db.scalars(select(Run).where(Run.job_id == job.id).order_by(Run.run_no.desc())).first()
        if run is None:
            continue
        for f in json.loads(run.result_json).get("fields", []):
            if f["group"] != "CERTIFIED_FIGURE" or f["status"] == "REJECTED":
                continue
            key = _norm(f["value"].get("covenant"))
            figures.setdefault(key, (f, job.document_version_id or job.id))
    out = []
    for c in body.covenants:
        metric = _best_metric(" ".join(filter(None, [c.covenantName, c.conditionExpression]))) or c.covenantName
        hit = figures.get(_norm(metric)) or figures.get(_norm(c.covenantName))
        if hit is None:
            continue
        f, version = hit
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
    return {"recommendations": out}
