"""Results (``dps.result/1.0``, contracts/schemas/result.json): every job's result is the run it stored.

Every job type runs in the skills flow and saves its result as a run (``dps_run``); a review saves a new run, and
the latest one is served. The adapter that rebuilt results of the old agreement and statement flows from their own
tables was removed with those tables (full review WP7, migration d4e7f1a2b3c9).
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps.models import Document, DocumentCpLink, Job, Run, StatementValue

SCHEMA = "dps.result/1.0"


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


def build(db: Session, job: Job, pipeline_version: str, store=None) -> dict:  # noqa: ARG001 - kept for callers
    """The job's latest run. A finished job always has one; without it (a job that ended before saving) the result is
    empty and held for a person, never invented."""
    saved = latest_run(db, job)
    if saved is not None:
        return saved
    from dps.skills.covenants.taxonomy import get as taxonomy

    doc = db.get(Document, job.document_id)
    return {
        "schema": SCHEMA, "runId": job.id, "jobId": job.id, "jobType": job.job_type,
        "status": "NEEDS_REVIEW" if job.status in ("NEEDS_REVIEW", "DONE") else job.status,
        "documentId": job.document_id, "documentVersionId": job.document_version_id,
        "sha256": job.sha256 or (doc.sha256 if doc else None),
        "pipeline": {"version": pipeline_version, "skillPack": None, "models": None,
                     "taxonomyVersion": taxonomy().version},
        "classification": None, "identifiers": [], "fields": [],
        "document": {"confidence": None, "route": "NEEDS_REVIEW", "reasons": ["no result was saved for this job"]},
        "warnings": [{"code": "NOTHING_FOUND", "message": "no result was saved for this job; a person reads it"}],
        "rollout": job.rollout, "timingMs": None, "usage": None,
    }
