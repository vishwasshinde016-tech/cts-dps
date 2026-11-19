"""Evidence, runs, ask, usage and the bank app's data-source contract (contracts/dps-jobs-api.yaml).

- ``GET /dps/v1/versions/{versionId}/pages/{page}/image`` (phase 2): a 150 dpi PNG of one page, rendered on demand
  and kept write-once under ``derived/<tenant>/<version>/pages/<n>.png``; the original file is never returned.
- ``GET /dps/v1/versions/{versionId}/runs`` (phase 4): every run on a version, newest first.
- ``POST /dps/v1/versions/{versionId}/ask`` (phase 4): a question answered from the page model with citations.
- ``GET /dps/v1/ops/usage`` (phase 3): model calls and tokens by day, tenant, job type and route.
- ``POST /api/data/values`` (phase 4): the bank app's DataApiClient contract, unchanged, from approved figures.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Date, cast, func, select

from dps import services
from dps.api.access import require_cp
from dps.api.auth import ADMIN, SERVICE, SUPPORT, require_roles
from dps.api.documents import DB, Checker, User, ValuesRequest
from dps.errors import ApiError, not_found
from dps.ingest.store import LocalStore, StoreError
from dps.models import AiUsage, Artifact, Document, Job, Run, utcnow

router = APIRouter()
IMAGE_DPI = 150


def _store(request: Request) -> LocalStore:
    s = request.app.state.settings
    return LocalStore(s.storage_dir, s.staging_dir, s.derived_dir, s.max_file_mb * 1024 * 1024)


def _version_jobs(db, version_id: str) -> list[Job]:
    from dps.api.access import same_tenant

    found = [j for j in db.scalars(select(Job).where(Job.document_version_id == version_id).order_by(Job.created_at))
             if same_tenant(j.tenant_id)]  # G2: another tenant's version does not exist here
    if not found:
        raise not_found("Version")
    return found


@router.get("/dps/v1/versions/{version_id}/pages/{page}/image")
def page_image(version_id: str, page: int, request: Request, db: DB, user: User) -> Response:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    if page < 1:
        raise ApiError(400, "INVALID_INPUT", "page starts at 1.")
    job = _version_jobs(db, version_id)[-1]
    doc = db.get(Document, job.document_id)
    if doc is None or not doc.object_key:
        raise not_found("Version")
    store = _store(request)
    tenant = job.tenant_id or "default"
    try:
        path = LocalStore.derived_path(tenant, version_id, "pages", f"{page}.png")
    except StoreError as ex:
        raise ApiError(400, "INVALID_INPUT", str(ex)) from ex
    art = db.scalars(select(Artifact).where(Artifact.path == path)).first()
    if art is not None:
        try:
            return Response(store.get_derived(art.path, art.sha256), media_type="image/png")
        except StoreError:
            pass  # rebuilt below
    try:
        data = store.open(doc.object_key, doc.sha256)
    except StoreError as ex:
        raise ApiError(404 if ex.code == "NOT_FOUND" else 409, ex.code, str(ex)) from ex
    if not data.startswith(b"%PDF"):
        raise ApiError(404, "NOT_FOUND", "Page images exist for PDF versions only.")
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    if page > len(pdf):
        raise ApiError(404, "NOT_FOUND", f"The version has {len(pdf)} page(s).")
    image = pdf[page - 1].render(scale=IMAGE_DPI / 72).to_pil()
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    png = out.getvalue()
    if not store.has_derived(path):
        rec = store.put_derived(tenant, version_id, path, png, "PAGE_IMAGE")
        db.add(Artifact(tenant_id=tenant, version_id=version_id, kind="PAGE_IMAGE", path=rec.path, sha256=rec.sha256,
                        bytes=rec.bytes, source_sha256=doc.sha256, pipeline_version=job.pipeline_version))
        db.commit()
    return Response(png, media_type="image/png")


@router.get("/dps/v1/versions/{version_id}/runs")
def version_runs(version_id: str, db: DB, user: User) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    found = _version_jobs(db, version_id)
    by_job = {j.id: j for j in found}
    runs = list(db.scalars(select(Run).where(Run.job_id.in_(list(by_job))).order_by(Run.created_at.desc())))
    out = []
    newest_per_job: dict[str, str] = {}
    for r in runs:
        body = json.loads(r.result_json)
        run_id = body.get("runId") or r.id
        job = by_job[r.job_id]
        out.append({"runId": run_id, "jobId": r.job_id, "jobType": job.job_type, "status": r.status,
                    "pipelineVersion": job.pipeline_version, "skillPack": (body.get("pipeline") or {}).get("skillPack"),
                    "startedAt": (job.started_at or r.created_at).isoformat(), "finishedAt": r.created_at.isoformat(),
                    "supersededBy": newest_per_job.get(r.job_id), "resultSha256": None, "trigger": r.trigger})
        newest_per_job.setdefault(r.job_id, run_id)
    for j in found:  # proven flows (agreement, statement) keep their results in the older tables: one run each
        if j.id not in newest_per_job and j.status in ("DONE", "NEEDS_REVIEW"):
            out.append({"runId": j.id, "jobId": j.id, "jobType": j.job_type, "status": j.status,
                        "pipelineVersion": j.pipeline_version, "skillPack": None,
                        "startedAt": (j.started_at or j.created_at).isoformat(),
                        "finishedAt": j.finished_at.isoformat() if j.finished_at else None, "supersededBy": None,
                        "resultSha256": None})
    return {"versionId": version_id, "runs": out}


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/dps/v1/versions/{version_id}/ask")
def ask_version(version_id: str, body: AskRequest, request: Request, db: DB, user: User) -> dict:
    require_roles(user, SERVICE, ADMIN, SUPPORT)
    from dps.advisory import agreement_qa
    from dps.parse.page_model import to_pages

    job = _version_jobs(db, version_id)[-1]
    art = db.scalars(select(Artifact).where(Artifact.source_sha256 == job.sha256, Artifact.kind == "PAGE_MODEL")
                     ).first()
    if art is None:
        raise ApiError(404, "RESULT_NOT_READY", "The version has not been read yet.")
    try:
        model = json.loads(gzip.decompress(_store(request).get_derived(art.path, art.sha256)))
    except StoreError as ex:
        raise ApiError(409, ex.code, str(ex)) from ex
    advisor = request.app.state.advisor
    completion = getattr(advisor, "completion", None) if advisor is not None else None
    reply = agreement_qa.answer(body.question, to_pages(model), completion)
    citations = [{"page": c["page"], "quote": c["quote"]} for c in reply.get("citations", []) if c.get("page")]
    texts = {p["no"]: " ".join((p.get("text") or "").split()).lower() for p in model.get("pages", [])}
    grounded = bool(citations) and all(" ".join(c["quote"].rstrip("…").split()).lower()[:60] in texts.get(c["page"], "")
                                       for c in citations)
    return {"answer": reply["answer"], "citations": citations, "grounded": grounded}


@router.get("/dps/v1/ops/usage")
def ops_usage(request: Request, db: DB, user: User,
              days: Annotated[int, Query(ge=1, le=366)] = 30) -> dict:
    require_roles(user, ADMIN, SUPPORT)
    settings = request.app.state.settings
    to = utcnow().date()
    since = to - timedelta(days=days - 1)
    route_of = _routes(settings)
    rows = db.execute(select(cast(AiUsage.at, Date), AiUsage.job_id, AiUsage.model, func.sum(AiUsage.calls),
                             func.sum(AiUsage.prompt_tokens), func.sum(AiUsage.completion_tokens),
                             func.sum(AiUsage.cost))
                      .where(AiUsage.at >= utcnow() - timedelta(days=days))
                      .group_by(cast(AiUsage.at, Date), AiUsage.job_id, AiUsage.model)).all()
    job_ids = {r[1] for r in rows if r[1]}
    jobs_by_id = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(job_ids)))} if job_ids else {}
    merged: dict[tuple, dict] = {}
    for day, job_id, model, calls, tin, tout, cost in rows:
        job = jobs_by_id.get(job_id)
        key = (str(day)[:10], job.tenant_id if job else settings.tenant_id, job.job_type if job else "INTERACTIVE",
               route_of.get(model, "strong"))
        row = merged.setdefault(key, {"day": key[0], "tenantId": key[1], "jobType": key[2], "route": key[3],
                                      "calls": 0, "tokensIn": 0, "tokensOut": 0, "costUsd": None})
        row["calls"] += int(calls or 0)
        row["tokensIn"] += int(tin or 0)
        row["tokensOut"] += int(tout or 0)
        if cost is not None:
            row["costUsd"] = round((row["costUsd"] or 0) + float(cost), 6)
    return {"from": since.isoformat(), "to": to.isoformat(), "rows": sorted(merged.values(), key=lambda r: r["day"])}


def _routes(settings) -> dict[str, str]:
    from dps.agent.gateway import Routes

    routes = Routes.load(getattr(settings, "ai_routes_file", "") or None, settings.ai_model)
    out: dict[str, str] = {}
    for name in ("vision", "small", "strong"):  # strong wins when one deployment serves several
        r = routes.routes.get(name)
        if r and r.deployment:
            out[r.deployment] = name
    return out


@router.post("/api/data/values")
def compat_data_values(body: ValuesRequest, db: DB, user: User, checker: Checker) -> dict:
    """The bank app's DataApiClient contract (DataSeriesResponse), unchanged: approved figures only."""
    require_cp(checker, user, body.cpId, read_only=True)
    return services.values_series(db, body.cpId, body.keys, body.to, body.from_)


__all__ = ["router", "date"]
