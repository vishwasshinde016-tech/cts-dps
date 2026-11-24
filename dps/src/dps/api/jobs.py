"""CTS job API, Phase 1 (contracts/dps-jobs-api.yaml): submit and follow jobs, batches, event feed, catalogue, ops.

Callers are the CTS Spring app and its DMS with a service token. This is the only way into DPS: the old Covenexa
routes (their own upload, review and publish) were deleted in the full review (G12, WP7).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from datetime import UTC, date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dps import feed, jobs, results, services
from dps.api.auth import ADMIN, SERVICE, SUPPORT, require_roles
from dps.api.deps import DB, User, job_view
from dps.errors import ApiError, not_found
from dps.models import Batch, Document, DocumentCpLink, Job, TenantLimit, utcnow
from dps.settings import Settings, llm_allowed

router = APIRouter()
log = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0.0-draft"
SCHEMA_NAMES = ("job-request", "job-status", "result", "page-model", "event", "review", "review-decisions",
                "compat-data-values", "compat-covenant-status")

# What each job type needs today. Types not listed answer 422 UNSUPPORTED_JOB_TYPE until their phase.
JOB_TYPES = {
    # One pipeline (full review WP7): every job type that reads a document runs in the skills flow.
    "EXTRACT_AGREEMENT": {"flow": "SKILL_PACK", "document_type": "LOAN_AGREEMENT", "phase": 1},
    "EXTRACT_STATEMENT": {"flow": "SKILL_PACK", "document_type": "FINANCIAL_STATEMENT", "phase": 1},
    "ANALYZE": {"flow": "ANALYZE", "document_type": "UNKNOWN", "phase": 3},
    # Phase 4: skill packs run by the generic flow (dps.skills.runner).
    "EXTRACT_SANCTION": {"flow": "SKILL_PACK", "document_type": "SANCTION_LETTER", "phase": 4},
    "EXTRACT_CERTIFICATE": {"flow": "SKILL_PACK", "document_type": "COMPLIANCE_CERTIFICATE", "phase": 4},
    "EXTRACT_AMENDMENT": {"flow": "SKILL_PACK", "document_type": "AMENDMENT", "phase": 4},
    "DRAFT_COMMENTARY": {"flow": "SKILL_PACK", "document_type": "COMMENTARY_MANIFEST", "phase": 4},
    # G1 (Miro onboarding flow): classify, then extract covenants and schedule terms with the right pack.
    "ONBOARD": {"flow": "SKILL_PACK", "document_type": "UNKNOWN", "phase": 4},
    # Full review WP9: waiver and consent letters; the covenants in force across a document set (JSON manifest).
    "EXTRACT_WAIVER": {"flow": "SKILL_PACK", "document_type": "WAIVER_CONSENT", "phase": 4},
    "RECONCILE_SET": {"flow": "SKILL_PACK", "document_type": "DOCUMENT_SET", "phase": 4},
    # CTS integration, 23 Nov 2026: one upload for every document type. DPS classifies the document and reads it
    # with the pack for its type: agreement, sanction letter, amendment, waiver, statement or compliance certificate.
    "AUTO": {"flow": "SKILL_PACK", "document_type": "UNKNOWN", "phase": 4},
}
JSON_TYPES = ("DRAFT_COMMENTARY", "RECONCILE_SET")  # their document is a JSON manifest, not a file to read
LATER_TYPES = {"REPROCESS": 5}
MEDIA_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/json": ".json",  # DRAFT_COMMENTARY's outcome manifest only
    "image/png": ".png",  # a photo or scan, read with OCR (CTS integration, 23 Nov 2026)
    "image/jpeg": ".jpg",
}


# ---------- request bodies (mirror contracts/schemas/job-request.json) ----------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentRef(_Strict):
    versionId: str | None = Field(default=None, max_length=64)  # noqa: N815 - contract field names
    objectKey: str | None = Field(default=None, max_length=500)  # noqa: N815
    stagingRef: str | None = Field(default=None, max_length=500)  # noqa: N815
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fileName: str = Field(min_length=1, max_length=255)  # noqa: N815
    mediaType: Literal["application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # noqa: N815
                       "application/vnd.ms-excel", "text/csv",
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       "application/json", "image/png", "image/jpeg"]
    sizeBytes: int = Field(ge=1)  # noqa: N815


class BaseCovenant(_Strict):
    """G2: an approved covenant from the CTS DB that an amendment is compared with (sent by Spring)."""
    appliedCovenantId: str = Field(min_length=1, max_length=64)  # noqa: N815
    name: str = Field(min_length=1, max_length=255)
    metric: str | None = Field(default=None, max_length=64)
    operator: Literal[">=", ">", "<=", "<", "="] | None = None
    threshold: float | None = None
    unit: str | None = Field(default=None, max_length=32)
    expression: str | None = Field(default=None, max_length=2000)
    clause: str | None = Field(default=None, max_length=32)
    category: Literal["FINANCIAL", "NON_FINANCIAL"] = "FINANCIAL"


class JobContext(_Strict):
    tenantId: str = Field(default="default", max_length=64)  # noqa: N815
    cpIds: list[str] = Field(default_factory=list, max_length=50)  # noqa: N815
    obligorId: str | None = Field(default=None, max_length=64)  # noqa: N815
    borrowerId: str | None = Field(default=None, max_length=64)  # noqa: N815
    facilityIds: list[str] = Field(default_factory=list, max_length=50)  # noqa: N815
    expectedType: str | None = None  # noqa: N815
    asOfDate: date | None = None  # noqa: N815
    basis: Literal["STANDALONE", "CONSOLIDATED"] | None = None
    # Spec Flow 3: what the assessment cycle expects; a document that shows something else goes to a person.
    legalEntity: str | None = Field(default=None, max_length=200)  # noqa: N815 - name or CIN
    evidenceType: str | None = Field(default=None, max_length=40)  # noqa: N815
    expectedCurrency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # noqa: N815
    expectedUnits: Literal["UNITS", "THOUSAND", "LAKH", "CRORE", "MILLION", "BILLION"] | None = None  # noqa: N815
    amendsVersionId: str | None = Field(default=None, max_length=64)  # noqa: N815
    restatesVersionId: str | None = Field(default=None, max_length=64)  # noqa: N815 - WP9 amended and restated
    baseCovenants: list[BaseCovenant] | None = Field(default=None, max_length=500)  # noqa: N815
    cycleId: str | None = Field(default=None, max_length=64)  # noqa: N815
    reprocessReason: str | None = Field(default=None, max_length=500)  # noqa: N815
    requestedBy: str = Field(min_length=1, max_length=255)  # noqa: N815


class JobRequest(_Strict):
    jobType: Literal["ANALYZE", "ONBOARD", "EXTRACT_AGREEMENT", "EXTRACT_AMENDMENT", "EXTRACT_SANCTION",  # noqa: N815
                     "EXTRACT_STATEMENT", "EXTRACT_CERTIFICATE", "DRAFT_COMMENTARY", "REPROCESS", "EXTRACT_WAIVER",
                     "RECONCILE_SET", "AUTO"]
    document: DocumentRef
    context: JobContext
    priority: Literal["INTERACTIVE", "AMENDMENT", "ASSESSMENT", "BULK", "REPROCESS"] = "INTERACTIVE"


class BatchItem(_Strict):
    clientFileReference: str = Field(min_length=1, max_length=128)  # noqa: N815
    job: JobRequest


class BatchRequest(_Strict):
    batchRef: str = Field(min_length=1, max_length=128)  # noqa: N815
    items: list[BatchItem] = Field(min_length=1)


# ---------- helpers ----------

def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _hash(body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_key(key: str | None) -> str:
    if not key or not 8 <= len(key) <= 128:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (8 to 128 characters).")
    return key


def _limits(db: Session, settings: Settings, tenant_id: str) -> TenantLimit:
    defaults = TenantLimit(tenant_id=tenant_id, max_queued=settings.queue_max_queued_per_tenant,
                           max_running=settings.queue_max_running_per_tenant,
                           max_running_per_batch=settings.queue_max_running_per_batch)
    return jobs.tenant_limits(db, tenant_id, defaults)


def _check_tenant(settings: Settings, tenant_id: str) -> None:
    if tenant_id != settings.tenant_id:
        raise ApiError(403, "TENANT_MISMATCH", "The request is for another tenant.")


def _accepted(job: Job, duplicate: bool) -> dict:
    return {"jobId": job.id, "status": job.status, "duplicate": duplicate,
            "links": {"self": f"/dps/v1/jobs/{job.id}", "result": f"/dps/v1/jobs/{job.id}/result"}}


def validate_job(store, body: JobRequest, settings: Settings) -> dict:
    """Checks a request that the schema accepts against the rules of Phase 1. Returns the job type's entry."""
    spec = JOB_TYPES.get(body.jobType)
    if spec is None:
        phase = LATER_TYPES.get(body.jobType)
        raise ApiError(422, "UNSUPPORTED_JOB_TYPE", f"{body.jobType} is not available yet"
                       + (f" (phase {phase})." if phase else "."), {"jobType": body.jobType})
    _check_tenant(settings, body.context.tenantId)
    if body.document.mediaType not in MEDIA_TYPES:
        raise ApiError(400, "UNSUPPORTED_FORMAT", f"{body.document.mediaType} cannot be processed yet.")
    if (body.document.mediaType == "application/json") != (body.jobType in JSON_TYPES):
        raise ApiError(400, "UNSUPPORTED_FORMAT", "application/json is the manifest of DRAFT_COMMENTARY and "
                                                  "RECONCILE_SET only.")
    # G18: a change is read against what was approved. A waiver needs the approved covenants; an amendment needs
    # them or the version it amends (DPS then compares with its own reading, flagged BASE_FROM_DPS).
    if body.jobType == "EXTRACT_WAIVER" and not body.context.baseCovenants:
        raise ApiError(400, "BASE_REQUIRED", "context.baseCovenants (the approved covenants) is required for "
                                             "EXTRACT_WAIVER.")
    if body.jobType == "EXTRACT_AMENDMENT" and not (body.context.baseCovenants or body.context.amendsVersionId):
        raise ApiError(400, "BASE_REQUIRED", "context.baseCovenants (preferred) or context.amendsVersionId is "
                                             "required for EXTRACT_AMENDMENT.")
    if body.document.sizeBytes > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(400, "INVALID_INPUT", f"The file is larger than {settings.max_upload_mb} MB.")
    if body.jobType == "ANALYZE" and not body.document.objectKey:
        # Phase 3: a staged file, not yet committed by the DMS, checked before commit.
        if not body.document.stagingRef:
            raise ApiError(400, "INVALID_INPUT", "document.stagingRef (or objectKey) is required for ANALYZE.")
        from dps.ingest.store import LocalStore, StoreError

        staged = LocalStore(settings.storage_dir, settings.staging_dir, settings.derived_dir,
                            settings.max_file_mb * 1024 * 1024)
        try:
            staged.open_staged(body.document.stagingRef, body.document.sha256)
        except StoreError as ex:
            code = ex.code if ex.code == "INTEGRITY_MISMATCH" else "INVALID_INPUT"
            raise ApiError(400, code, f"document.stagingRef: {ex}") from ex
        return spec
    if (body.jobType != "ANALYZE" and not body.document.versionId) or not body.document.objectKey:
        raise ApiError(400, "INVALID_INPUT", "document.versionId and document.objectKey are required for "
                                             f"{body.jobType}.")
    # Phase 1 reads the bytes from DPS's own store; Phase 2 reads the DMS store through the same key.
    try:
        path = store.path(body.document.objectKey)
    except ValueError as ex:
        raise ApiError(400, "INVALID_INPUT", "document.objectKey is not a valid key.") from ex
    if not path.is_file():
        raise ApiError(400, "INVALID_INPUT", "No file is stored under document.objectKey.")
    # Streamed, with the observed size checked first: a submit never holds a whole file in memory (audit F07).
    if path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(400, "INVALID_INPUT", f"The stored file is larger than {settings.max_upload_mb} MB.")
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != body.document.sha256:
        raise ApiError(400, "INTEGRITY_MISMATCH", "The stored file does not match document.sha256.")
    return spec


def _document_for(db: Session, body: JobRequest, spec: dict) -> Document:
    ctx, ref = body.context, body.document
    if ref.versionId:
        previous = db.scalars(select(Document).where(Document.tenant_id == ctx.tenantId,
                                                     Document.external_version_id == ref.versionId)).first()
        if previous is not None and (previous.sha256, previous.object_key) != (ref.sha256, ref.objectKey):
            raise ApiError(400, "INTEGRITY_MISMATCH", "The document version already refers to a different object.")
    # Local state is scoped to the job. The immutable DMS object remains shared.
    doc = Document(type=spec["document_type"], file_name=ref.fileName,
                   storage_path=ref.objectKey or ref.stagingRef or "",
                   sha256=ref.sha256, size_bytes=ref.sizeBytes, as_of_date=ctx.asOfDate, status="QUEUED",
                   uploaded_by=ctx.requestedBy, tenant_id=ctx.tenantId, external_version_id=ref.versionId,
                   object_key=ref.objectKey)
    db.add(doc)
    db.flush()
    for cp_id in sorted(set(ctx.cpIds)):
        db.add(DocumentCpLink(document_id=doc.id, cp_id=cp_id, linked_by=ctx.requestedBy))
    return doc


def create_job(db: Session, body: JobRequest, spec: dict, settings: Settings, *, key: str | None,
               request_hash: str, correlation_id: str | None, batch_id: str | None = None,
               client_ref: str | None = None, principal=None) -> Job:
    """Adds the scoped document record, job and accepted event. The caller commits."""
    doc = _document_for(db, body, spec)
    priority = jobs.PRIORITY[body.priority]
    job = Job(document_id=doc.id, flow=spec["flow"], job_type=body.jobType, tenant_id=body.context.tenantId,
              priority=priority, due_at=utcnow(), idempotency_key=key, request_hash=request_hash,
              input_key=_input_key(body, settings.pipeline_version, _rollout(body, settings)),
              request_json=body.model_dump_json(), correlation_id=correlation_id, batch_id=batch_id,
              client_file_reference=client_ref, pipeline_version=settings.pipeline_version,
              document_version_id=body.document.versionId, sha256=body.document.sha256,
              requested_by=body.context.requestedBy,
              rollout=_rollout(body, settings))
    db.add(job)
    db.flush()
    doc.latest_job_id = job.id
    feed.emit_for_job(db, "dps.job.accepted", job,
                      {"jobType": job.job_type, "priority": body.priority, "batchId": batch_id})
    services.audit(db, body.context.requestedBy, "JOB_SUBMIT", "job", job.id, jobType=job.job_type,
                   versionId=job.document_version_id, batchId=batch_id,
                   principal=getattr(principal, "user", None))  # G4: the caller, beside the asserted user
    return job


def _rollout(body: JobRequest, settings: Settings) -> str:
    return settings.rollout_modes.get(body.jobType, settings.rollout_default)


def _input_key(body: JobRequest, pipeline: str, rollout: str) -> str:
    context = body.context.model_dump(mode="json", exclude={"requestedBy"})
    for field in ("cpIds", "facilityIds"):
        context[field] = sorted(set(context[field]))
    if context["baseCovenants"] is not None:
        context["baseCovenants"] = sorted(
            context["baseCovenants"], key=lambda covenant: covenant["appliedCovenantId"])
    ref = body.document
    data = [body.jobType, pipeline, rollout, ref.versionId, ref.objectKey, ref.sha256, ref.mediaType,
            ref.fileName.rsplit(".", 1)[-1].lower(), context]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


FAILED_ENDS = (jobs.FAILED, jobs.DEAD, jobs.CANCELLED)


def _same_input(db: Session, body: JobRequest, settings: Settings) -> Job | None:
    """The job that already read this input (same version, type, context and pipeline), or None. A job that ended
    FAILED, DEAD or CANCELLED does not count: the same document may be sent again (CTS review, 23 Nov 2026: it was
    stuck for ever). Its input key is released so the new job can take it (the key is unique)."""
    key = _input_key(body, settings.pipeline_version, _rollout(body, settings))
    tenant = Job.tenant_id == body.context.tenantId
    existing = db.scalars(select(Job).where(tenant, Job.input_key == key)).first()
    if existing is not None and existing.status in FAILED_ENDS:
        existing.input_key = None
        db.flush()
        return None
    if existing is not None:
        return existing
    # Preserve equivalent historical jobs without hashing incomplete legacy request records.
    older = db.scalars(select(Job).where(
        tenant, Job.input_key.is_(None), Job.job_type == body.jobType, Job.sha256 == body.document.sha256,
        Job.pipeline_version == settings.pipeline_version, Job.document_version_id == body.document.versionId,
        Job.request_json.is_not(None)))
    for job in older:
        try:
            previous = JobRequest.model_validate_json(job.request_json)
        except ValueError:
            continue
        if _input_key(previous, job.pipeline_version, job.rollout) == key and job.status not in FAILED_ENDS:
            return job
    return None


def _job_or_404(db: Session, job_id: str) -> Job:
    from dps.api.access import same_tenant

    job = db.get(Job, job_id)
    if job is None or not same_tenant(job.tenant_id):  # G2: another tenant's job does not exist here
        raise not_found("Job")
    return job


# ---------- jobs ----------

@router.post("/dps/v1/jobs", status_code=202)
def submit_job(body: JobRequest, request: Request, response: Response, db: DB, user: User,
               idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN)
    settings = _settings(request)
    key = _require_key(idempotency_key)
    request_hash = _hash(body)
    tenant = body.context.tenantId
    existing = db.scalars(select(Job).where(Job.tenant_id == tenant, Job.idempotency_key == key)).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different request.")
        response.status_code = 200
        return _accepted(existing, duplicate=True)
    spec = validate_job(request.app.state.store, body, settings)
    same = _same_input(db, body, settings)
    if same is not None:  # the same version, type and pipeline is processed once
        response.status_code = 200
        return _accepted(same, duplicate=True)
    if jobs.waiting_count(db, tenant) >= _limits(db, settings, tenant).max_queued:
        raise ApiError(429, "QUEUE_FULL", "Too many jobs are waiting. Try again later.", {"retryAfterSeconds": 30})
    try:
        job = create_job(db, body, spec, settings, key=key, request_hash=request_hash,
                         correlation_id=getattr(request.state, "correlation_id", None), principal=user)
        db.commit()
    except IntegrityError:  # a concurrent request with the same key or input won the race
        db.rollback()
        winner = db.scalars(select(Job).where(Job.tenant_id == tenant, Job.idempotency_key == key)).first() \
            or _same_input(db, body, settings)
        if winner is None:
            raise
        if winner.idempotency_key == key and winner.request_hash != request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED",
                           "This Idempotency-Key was used for a different request.") from None
        response.status_code = 200
        return _accepted(winner, duplicate=True)
    response.headers["Location"] = f"/dps/v1/jobs/{job.id}"
    return _accepted(job, duplicate=False)


@router.get("/dps/v1/jobs/{job_id}/result")
def job_result(job_id: str, request: Request, db: DB, user: User) -> Response:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job_or_404(db, job_id)
    if job.status == jobs.CANCELLED:
        raise ApiError(410, "JOB_CANCELLED", "The job was cancelled.")
    if job.status not in (jobs.DONE, jobs.NEEDS_REVIEW):
        raise ApiError(404, "RESULT_NOT_READY", f"The job is {job.status}.", {"status": job.status})
    settings = _settings(request)
    from dps.ingest.store import LocalStore

    derived = LocalStore(settings.storage_dir, settings.staging_dir, settings.derived_dir)
    body = results.build(db, job, settings.pipeline_version, derived)
    raw = json.dumps(body, default=str, separators=(",", ":"))
    etag = '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(raw, media_type="application/json", headers={"ETag": etag})


@router.get("/health")
def health(request: Request) -> dict:
    """Liveness with the database and the version (the load balancer's check)."""
    from dps import __version__

    db_status = "UP"
    try:
        with request.app.state.db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as ex:  # noqa: BLE001 - a failed dependency check returns degraded health
        log.debug("Health database probe failed: %s", type(ex).__name__)
        db_status = "DOWN"
    return {"status": "UP" if db_status == "UP" else "DEGRADED", "database": db_status, "version": __version__}


@router.get("/dps/v1/jobs")
def list_jobs(db: DB, user: User, status: str | None = None, jobType: str | None = None,  # noqa: N803
              batchId: str | None = None, limit: int = 100) -> list[dict]:  # noqa: N803 - contract names
    """Job operations for support (contract listJobs): newest first, optionally one status (e.g. FAILED), one job
    type or one batch, this deployment's tenant only (G2), with the file name and CPs that say which upload it is."""
    from dps.api.access import tenant

    require_roles(user, SUPPORT, ADMIN)
    q = select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500)))
    if tenant():
        q = q.where(Job.tenant_id == tenant())
    if status:
        q = q.where(Job.status == status.upper())
    if jobType:
        q = q.where(Job.job_type == jobType.upper())
    if batchId:
        q = q.where(Job.batch_id == batchId)
    found = list(db.scalars(q))
    ids = sorted({j.document_id for j in found})
    docs = {d.id: d for d in db.scalars(select(Document).where(Document.id.in_(ids)))} if ids else {}
    return [{**job_view(j), "fileName": docs[j.document_id].file_name if j.document_id in docs else None,
             "cpIds": docs[j.document_id].active_cp_ids() if j.document_id in docs else []} for j in found]


@router.get("/dps/v1/jobs/{job_id}")
def get_job_status(job_id: str, db: DB, user: User) -> dict:
    """Job status for Spring (contract getJob)."""
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    return job_view(_job_or_404(db, job_id))


@router.post("/dps/v1/jobs/{job_id}/retry", status_code=202)
def retry_failed_job(job_id: str, db: DB, user: User) -> dict:
    """Support puts a FAILED or DEAD job back in the queue with fresh attempts (contract retryJob); audited."""
    require_roles(user, SUPPORT, ADMIN)
    job = _job_or_404(db, job_id)
    if job.status not in (jobs.FAILED, jobs.DEAD):
        raise ApiError(409, "JOB_NOT_RETRYABLE", f"Job is {job.status}, not FAILED or DEAD")
    now = utcnow()
    previous = job.status
    job.status, job.attempts, job.error, job.error_code = jobs.QUEUED, 0, None, None
    job.crash_count, job.due_at, job.finished_at, job.updated_at = 0, now, None, now
    job.cancel_requested = False
    services.audit(db, user.user, "JOB_RETRY", "job", job.id)
    jobs.requeued(db, job, user.user, previous)
    db.commit()
    return job_view(job)


@router.post("/dps/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, db: DB, user: User,
               x_user: Annotated[str | None, Header(alias="X-User")] = None) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    job = _job_or_404(db, job_id)
    from dps.api.review import acting_user

    actor = acting_user(x_user, user)
    try:
        jobs.request_cancel(db, job, actor)
    except jobs.JobFinished as ex:
        if str(ex) == jobs.NEEDS_REVIEW:
            raise ApiError(409, "JOB_FINISHED", "The job is waiting for review: its result is kept. Close the "
                                                "review (POST /dps/v1/jobs/{jobId}/close) instead.") from ex
        raise ApiError(409, "JOB_FINISHED", f"The job has already finished ({ex}).") from ex
    services.audit(db, actor, "JOB_CANCEL", "job", job.id)
    db.commit()
    return job_view(job)


# ---------- batches ----------

def _batch_view(db: Session, batch: Batch) -> dict:
    items = db.scalars(select(Job).where(Job.batch_id == batch.id).order_by(Job.created_at)).all()
    counts: dict[str, int] = {}
    rows = []
    for item in items:
        outcome = jobs.item_outcome(item)
        counts[outcome] = counts.get(outcome, 0) + 1
        rows.append({"clientFileReference": item.client_file_reference, "jobId": item.id, "outcome": outcome,
                     "errorCode": item.error_code})
    return {"batchId": batch.id, "batchRef": batch.batch_ref, "status": batch.status, "counts": counts,
            "items": rows}


@router.post("/dps/v1/batches", status_code=202)
def submit_batch(body: BatchRequest, request: Request, response: Response, db: DB, user: User,
                 idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> dict:
    """Flow 4: one transaction per item, so a bad file is recorded as REJECTED and never stops the others."""
    require_roles(user, SERVICE, ADMIN)
    settings = _settings(request)
    key = _require_key(idempotency_key)
    if len(body.items) > settings.batch_max_items:
        raise ApiError(400, "BATCH_TOO_LARGE", f"A batch holds at most {settings.batch_max_items} items.")
    tenants = {item.job.context.tenantId for item in body.items}
    if len(tenants) != 1:
        raise ApiError(400, "INVALID_INPUT", "All items of a batch must be for one tenant.")
    tenant = tenants.pop()
    _check_tenant(settings, tenant)
    request_hash = _hash(body)
    existing = db.scalars(select(Batch).where(Batch.tenant_id == tenant, Batch.idempotency_key == key)).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different batch.")
        response.status_code = 200
        return {"batchId": existing.id, "itemCount": existing.item_count, "status": existing.status,
                "duplicate": True}
    limit = _limits(db, settings, tenant).max_queued
    if jobs.waiting_count(db, tenant) + len(body.items) > limit:
        raise ApiError(429, "QUEUE_FULL", "Too many jobs are waiting. Try again later.", {"retryAfterSeconds": 60})
    requested_by = body.items[0].job.context.requestedBy
    batch = Batch(tenant_id=tenant, batch_ref=body.batchRef, idempotency_key=key, request_hash=request_hash,
                  item_count=len(body.items), created_by=requested_by)
    db.add(batch)
    db.commit()
    cid = getattr(request.state, "correlation_id", None)
    for item in body.items:
        _submit_item(db, request, settings, batch, item, cid)
    db.refresh(batch)
    jobs_any = db.scalars(select(Job).where(Job.batch_id == batch.id)).first()
    if jobs_any is not None:  # completes the batch at once when every item was refused or a duplicate
        jobs._sync_batch(db, jobs_any)  # noqa: SLF001 - same module family
        db.commit()
    return {"batchId": batch.id, "itemCount": batch.item_count, "status": batch.status, "duplicate": False}


def _submit_item(db: Session, request: Request, settings: Settings, batch: Batch, item: BatchItem,
                 cid: str | None) -> None:
    body = item.job
    request_hash = _hash(body)
    try:
        spec = validate_job(request.app.state.store, body, settings)
    except ApiError as ex:  # recorded as a REJECTED item, the batch goes on
        _refused_item(db, batch, item, request_hash, ex.code, ex.message, cid)
        return
    same = _same_input(db, body, settings)
    try:
        if same is not None:
            _refused_item(db, batch, item, request_hash, "DUPLICATE", f"Same input as job {same.id}", cid,
                          status=jobs.DONE)
            return
        create_job(db, body, spec, settings, key=None, request_hash=request_hash, correlation_id=cid,
                   batch_id=batch.id, client_ref=item.clientFileReference)
        db.commit()
    except IntegrityError:
        db.rollback()
        _refused_item(db, batch, item, request_hash, "DUPLICATE", "Same input as an earlier job", cid,
                      status=jobs.DONE)
    except ApiError as ex:
        db.rollback()
        _refused_item(db, batch, item, request_hash, ex.code, ex.message, cid)


def _refused_item(db: Session, batch: Batch, item: BatchItem, request_hash: str, code: str, message: str,
                  cid: str | None, status: str = jobs.FAILED) -> None:
    """A batch item that never reaches the queue still gets a job row, so the batch report lists it."""
    body = item.job
    doc = Document(type=JOB_TYPES.get(body.jobType, {}).get("document_type", "EVIDENCE"),
                   file_name=body.document.fileName, storage_path="", sha256=body.document.sha256,
                   size_bytes=body.document.sizeBytes, status="FAILED" if status == jobs.FAILED else "PUBLISHED",
                   uploaded_by=body.context.requestedBy, tenant_id=body.context.tenantId)
    db.add(doc)
    db.flush()
    now = utcnow()
    job = Job(document_id=doc.id, flow=JOB_TYPES.get(body.jobType, {}).get("flow", body.jobType),
              job_type=body.jobType, tenant_id=body.context.tenantId, status=status, error=message[:2000],
              error_code=code, priority=jobs.PRIORITY[body.priority], request_hash=request_hash,
              request_json=body.model_dump_json(), correlation_id=cid, batch_id=batch.id,
              client_file_reference=item.clientFileReference, pipeline_version="refused",
              sha256=body.document.sha256, requested_by=body.context.requestedBy, finished_at=now, due_at=now)
    db.add(job)
    db.commit()


def _batch_or_404(db: Session, batch_id: str) -> Batch:
    """G2 for every batch endpoint: another tenant's batch does not exist here (errors.csv and rerun skipped this
    check before; Phase 1 review, 22 Nov 2026)."""
    from dps.api.access import same_tenant

    batch = db.get(Batch, batch_id)
    if batch is None or not same_tenant(batch.tenant_id):
        raise not_found("Batch")
    return batch


@router.get("/dps/v1/batches/{batch_id}")
def get_batch(batch_id: str, db: DB, user: User) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    return _batch_view(db, _batch_or_404(db, batch_id))


@router.get("/dps/v1/batches/{batch_id}/errors.csv")
def batch_errors(batch_id: str, db: DB, user: User) -> Response:
    """Codes and references only: never document content or customer names."""
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    batch = _batch_or_404(db, batch_id)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["clientFileReference", "jobId", "outcome", "errorCode"])
    for row in _batch_view(db, batch)["items"]:
        if row["outcome"] in ("REJECTED", "FAILED_RETRYABLE", "PENDING_MAPPING"):
            writer.writerow([row["clientFileReference"], row["jobId"], row["outcome"], row["errorCode"] or ""])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="batch-{batch_id}-errors.csv"'})


@router.post("/dps/v1/batches/{batch_id}/rerun", status_code=202)
def rerun_batch(batch_id: str, db: DB, user: User) -> dict:
    """Re-queues only the items that failed after reaching the queue (DEAD); refused items need a new file."""
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    batch = _batch_or_404(db, batch_id)
    now = utcnow()
    rerun = db.scalars(select(Job).where(Job.batch_id == batch.id, Job.status == jobs.DEAD)).all()
    for job in rerun:
        job.status, job.attempts, job.error, job.error_code = jobs.QUEUED, 0, None, None
        job.crash_count, job.due_at, job.finished_at, job.updated_at = 0, now, None, now
        jobs.requeued(db, job, user.user, jobs.DEAD)
    if rerun:
        batch.status, batch.finished_at = "RUNNING", None
    services.audit(db, user.user, "BATCH_RERUN", "batch", batch.id, items=len(rerun))
    db.commit()
    return _batch_view(db, batch)


# ---------- events ----------

@router.get("/dps/v1/events")
def read_events(db: DB, user: User, after: int = 0, limit: int = 100) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    from dps.api.access import tenant

    events, cursor = feed.read(db, max(0, after), limit, tenant_id=tenant())  # G2: this tenant's events only
    return {"events": events, "nextCursor": cursor}


# ---------- catalogue ----------

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"


def _contract_file(*parts: str) -> Path:
    return CONTRACT_DIR.joinpath(*parts)


@router.get("/dps/v1/schemas")
def list_schemas(user: User) -> list[dict]:  # noqa: ARG001 - authentication only
    out = []
    for name in SCHEMA_NAMES:
        schema = json.loads(_contract_file("schemas", f"{name}.json").read_text(encoding="utf-8"))
        out.append({"name": name, "version": schema["title"].split("/")[-1], "url": f"/dps/v1/schemas/{name}"})
    return out


@router.get("/dps/v1/schemas/{name}")
def get_schema(name: str, user: User) -> Response:  # noqa: ARG001 - authentication only
    if name not in SCHEMA_NAMES:
        raise not_found("Schema")
    raw = _contract_file("schemas", f"{name}.json").read_text(encoding="utf-8")
    return Response(raw, media_type="application/schema+json")


@router.get("/dps/v1/capabilities")
def capabilities(request: Request, user: User) -> dict:  # noqa: ARG001 - authentication only
    settings = _settings(request)
    types = [{"jobType": t, "available": True, "phase": spec["phase"]} for t, spec in JOB_TYPES.items()]
    types += [{"jobType": t, "available": False, "phase": phase} for t, phase in LATER_TYPES.items()]
    return {"jobTypes": types, "mediaTypes": sorted(MEDIA_TYPES),
            "limits": {"maxUploadMb": settings.max_upload_mb, "maxPages": settings.max_pages,
                       "batchMaxItems": settings.batch_max_items,
                       "maxQueuedPerTenant": settings.queue_max_queued_per_tenant},
            "pipelineVersion": settings.pipeline_version, "contractVersion": CONTRACT_VERSION,
            "llmEnabled": llm_allowed(settings)}


# ---------- ops ----------

@router.get("/health/live")
def live() -> dict:
    return {"status": "UP"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    checks = {}
    try:
        with request.app.state.db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "UP"
    except Exception as ex:  # noqa: BLE001 - readiness reports unavailable dependencies
        log.debug("Readiness database probe failed: %s", type(ex).__name__)
        checks["database"] = "DOWN"
    checks["storage"] = "UP" if request.app.state.store.root.is_dir() else "DOWN"
    up = all(v == "UP" for v in checks.values())
    # G5: is any worker alive? The leader renews the scheduler lock every 10 s; an expired lock means no worker is
    # processing. Reported for operations and alerts, not used for this API's own readiness.
    workers = "UNKNOWN"
    if checks["database"] == "UP":
        try:
            from dps.models import Leader

            with request.app.state.db.session_factory() as db:
                lock = db.get(Leader, "scheduler")
            if lock is not None:
                until = lock.until if lock.until.tzinfo else lock.until.replace(tzinfo=UTC)
                workers = "UP" if until >= utcnow() else "DOWN"
        except Exception as ex:  # noqa: BLE001 - worker status is informational
            log.debug("Readiness worker probe failed: %s", type(ex).__name__)
            workers = "UNKNOWN"
    settings = _settings(request)
    spring = "NOT_CONFIGURED"
    if settings.core_health_path:
        from dps.integrations.spring import SpringClient

        with httpx.Client(base_url=settings.core_base_url, timeout=2.0) as client:
            spring = SpringClient(settings.core_base_url, 2.0, settings.core_service_token,
                                  client=client).health(settings.core_health_path)
    return JSONResponse({"status": "UP" if up else "DOWN", **checks, "workers": workers,
                         "spring": spring,
                         "parser": _parser_state(settings.parser_socket),
                         "model": _model_state(request, settings) if checks["database"] == "UP" else "UNKNOWN"},
                        status_code=200 if up else 503)


MODEL_WINDOW_SECONDS = 300  # the model's recent calls that say whether it answers


def _parser_state(socket_path: str) -> str:
    """G5: the parser container's socket answers (UP), is gone (DOWN), or the worker parses in a child process
    (IN_PROCESS, no socket configured). Informational, like the workers: the API itself does not parse."""
    if not socket_path:
        return "IN_PROCESS"
    import socket

    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return "UNKNOWN"
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(socket_path)
        return "UP"
    except OSError:
        return "DOWN"
    finally:
        sock.close()


def _model_state(request: Request, settings: Settings) -> str:
    """G5: the model routes as the workers last saw them: OFF (switched off), IDLE (no call in the last 5 minutes),
    UP, DEGRADED (some calls failed: the breaker may open) or DOWN (every recent call failed). From the call log,
    because the breakers live in the workers."""
    if not llm_allowed(settings):
        return "OFF"
    from datetime import timedelta

    from dps.models import AiCall

    try:
        with request.app.state.db.session_factory() as db:
            recent = list(db.scalars(select(AiCall.status).where(
                AiCall.at >= utcnow() - timedelta(seconds=MODEL_WINDOW_SECONDS)).order_by(AiCall.at.desc()).limit(20)))
    except Exception as ex:  # noqa: BLE001 - recent AI status is informational
        log.debug("Readiness AI probe failed: %s", type(ex).__name__)
        return "UNKNOWN"
    if not recent:
        return "IDLE"
    failed = sum(1 for s in recent if s != "OK")
    if failed == len(recent) and failed >= 3:
        return "DOWN"
    return "DEGRADED" if failed else "UP"


@router.get("/dps/v1/ops/scaling")
def scaling_signals(db: DB, user: User) -> dict:
    """Phase 6: per pool, oldest queued age and depth per worker, with the scale-out / scale-in advice."""
    require_roles(user, SUPPORT, ADMIN)
    from dps.ops import production as ops

    return {"generatedAt": utcnow().isoformat(), "pools": ops.scaling(db)}


class RoutePauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool
    reason: str = Field(min_length=1, max_length=500, pattern=r"\S")


@router.put("/dps/v1/ops/ai-routes/{route}")
def pause_ai_route(route: str, body: RoutePauseRequest, request: Request, db: DB, user: User) -> dict:
    """A shared operator control checked before each model call, including retries."""
    from dps.agent.gateway import Routes
    from dps.models import AiRoutePause

    require_roles(user, ADMIN)
    settings = _settings(request)
    if len(route) > 64 or route not in Routes.load(settings.ai_routes_file or None, settings.ai_model).routes:
        raise not_found("AI route")
    current = db.get(AiRoutePause, route)
    if body.paused and current is None:
        db.add(AiRoutePause(route=route))
    elif not body.paused and current is not None:
        db.delete(current)
    services.audit(db, user.user, "AI_ROUTE_PAUSE" if body.paused else "AI_ROUTE_RESUME", "ai_route", route,
                   reason=body.reason)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "INVALID_INPUT", "The AI route state changed. Reload it before retrying.") from exc
    return {"route": route, "paused": body.paused}


@router.get("/dps/v1/ops/ai-routes")
def ai_routes(request: Request, db: DB, user: User) -> dict:
    from dps.agent.gateway import Routes
    from dps.models import AiRoutePause

    require_roles(user, SUPPORT, ADMIN)
    settings = _settings(request)
    paused = set(db.scalars(select(AiRoutePause.route)))
    return {"items": [{"route": r.name, "deployment": r.deployment, "paused": r.name in paused}
                      for r in Routes.load(settings.ai_routes_file or None, settings.ai_model).routes.values()]}


@router.get("/dps/v1/ops/affected-results")
def affected_results(db: DB, user: User, route: str | None = None, model: str | None = None,
                     pipelineVersion: str | None = None, after: str | None = None) -> dict:  # noqa: N803
    """Find current runs with the deployment, route or pipeline specified by an operator."""
    from dps.api.access import tenant
    from dps.models import Run

    require_roles(user, SUPPORT, ADMIN)
    if not (route or model or pipelineVersion):
        raise ApiError(400, "INVALID_INPUT", "Specify an AI route, model or pipeline version.")
    query = (select(Run, Job.pipeline_version).join(Job, Job.id == Run.job_id)
             .where(Run.superseded_by.is_(None), Job.tenant_id == (tenant() or "default")))
    if after:
        query = query.where(Run.id > after)
    batch = db.execute(query.order_by(Run.id).limit(501)).all()
    items = []
    for run, version in batch[:500]:
        result = json.loads(run.result_json)
        pipeline = result.get("pipeline") or {}
        models = pipeline.get("models") or {}
        version = pipeline.get("version") or version
        if ((route and route not in models) or (model and model not in models.values())
                or (pipelineVersion and version != pipelineVersion)):
            continue
        items.append({"jobId": run.job_id, "runId": result.get("runId") or run.job_id,
                      "pipelineVersion": version, "documentType": (result.get("classification") or {}).get("type")})
    return {"items": items, "nextCursor": batch[499][0].id if len(batch) > 500 else None}


@router.get("/dps/v1/ops/audit-seals")
def audit_seals(db: DB, user: User) -> dict:
    """Phase 6: the daily seals and whether the hash chain still matches the rows."""
    require_roles(user, SUPPORT, ADMIN)
    from dps.models import AuditSeal
    from dps.ops import production as ops

    seals = db.scalars(select(AuditSeal).order_by(AuditSeal.day.desc()).limit(60)).all()
    return {"intact": not ops.verify_seals(db), "problems": ops.verify_seals(db),
            "seals": [{"day": x.day.isoformat(), "rows": x.rows, "headHash": x.head_hash} for x in seals]}


@router.get("/dps/v1/ops/queues")
def queues(db: DB, user: User) -> dict:
    require_roles(user, SUPPORT, ADMIN)
    from dps.ops.production import review_backlog

    return {"generatedAt": utcnow().isoformat(), "rows": jobs.queue_report(db),
            "dead": db.scalar(select(func.count()).select_from(Job).where(Job.status == jobs.DEAD)) or 0,
            "review": review_backlog(db)}
