"""The ANALYZE job (Phase 3): classify and identify a staged or committed file from its page model.

Output: a ``dps.result/1.0`` run with ``classification`` and ``identifiers`` (no fields), and the
``dps.analysis.completed`` event with type, confidence, mismatch, identifiers and legibility. A type mismatch
or an uncertain type ends the job NEEDS_REVIEW so the DMS shows it to the user before commit.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dps import feed, jobs
from dps.analyze import obligations
from dps.analyze.classify import classify
from dps.analyze.identify import find_identifiers, legibility, match, missing_required
from dps.analyze.keypages import key_pages
from dps.models import Job, Run

SCHEMA = "dps.result/1.0"


def analyze(model: dict, job: Job, gateway=None, tools=None, pipeline_version: str = "1",
            loan_account_pattern: str | None = None, facility_pattern: str | None = None) -> dict:
    started = time.monotonic()
    request = json.loads(job.request_json or "{}")
    ctx = request.get("context") or {}
    expected = ctx.get("expectedType") or ctx.get("evidenceType")
    budget = gateway.begin_job() if gateway is not None else None
    try:
        c = classify(model, expected, gateway)
    finally:
        if gateway is not None:
            gateway.end_job()
    classified = time.monotonic()
    found = find_identifiers(model, loan_account_pattern, facility_pattern)
    lookup_ctx = {"cpIds": ctx.get("cpIds") or [], "obligorId": ctx.get("obligorId"),
                  "facilityIds": ctx.get("facilityIds") or []}
    identifiers, warnings = match(found, tools, lookup_ctx, job.correlation_id)
    reasons = []
    if c.mismatch:
        reasons.append(f"TYPE_MISMATCH: chosen {expected}, document reads as {c.type}")
        warnings.append({"code": "TYPE_MISMATCH", "message": reasons[-1]})
    if c.disputed:
        reasons.append(f"TYPE_DISPUTED: the rules read {c.type}; the Document Agent read {c.disputed}")
        warnings.append({"code": "TYPE_DISPUTED", "message": reasons[-1]})
    elif c.uncertain:
        reasons.append(f"UNCERTAIN_TYPE: best guess {c.type} at {c.confidence:.0%}"
                       + (" (read from the page images)" if c.method == "VISION" else ""))
        warnings.append({"code": "UNCERTAIN_TYPE", "message": reasons[-1]})
    missing = missing_required(c.type, found)
    if missing:  # G3: reported for the reviewer; a missing field is never guessed
        warnings.append({"code": "REQUIRED_MISSING",
                         "message": f"{c.type} usually shows: " + ", ".join(missing) + " (not found)"})
    conflicts = [i for i in identifiers if i["match"]["status"] == "CONFLICT"]
    if conflicts:
        reasons.append(f"MAPPING_CONFLICT: {len(conflicts)} identifier(s) point to another borrower or facility")
        warnings.append({"code": "MAPPING_CONFLICT", "message": reasons[-1]})
    mismatches = obligations.check(ctx, found, c.basis, model)  # spec Flow 3: what the cycle expects
    for w in mismatches:
        reasons.append(f"{w['code']}: {w['message']}")
    warnings += mismatches
    flagged = c.mismatch or c.uncertain or bool(conflicts) or bool(mismatches)
    route = obligations.route_for(warnings) if flagged else "AUTO_ACCEPTED"
    result = {
        "schema": SCHEMA, "runId": job.id, "jobId": job.id, "jobType": job.job_type,
        "status": "NEEDS_REVIEW" if flagged else "DONE",
        "documentId": job.document_id, "documentVersionId": job.document_version_id, "sha256": job.sha256,
        "pipeline": {"version": pipeline_version, "skillPack": "analyze/1",
                     "models": dict(budget.models) if budget and budget.models else None},
        "classification": c.as_result(),
        "identifiers": identifiers,
        "fields": [],
        "document": {"confidence": c.confidence, "route": route, "reasons": reasons},
        "warnings": warnings,
        "timingMs": {"classify": round((classified - started) * 1000, 1),
                     "identify": round((time.monotonic() - classified) * 1000, 1)},
        "usage": {"model_calls": budget.calls, "tokens": budget.used} if budget else None,
        "legibility": legibility(model),
        "keyPages": key_pages(model, c.type),
        "segments": model.get("segments", []),
        "flags": model.get("flags", []),
    }
    if c.evidence_kind:
        result["classification"]["evidenceKind"] = c.evidence_kind
    return result


def event_payload(result: dict) -> dict:
    c = result["classification"]
    return {"type": c["type"], "confidence": c["confidence"], "mismatch": c["mismatch"],
            "uncertain": c["confidence"] < 0.6, "expected": c.get("expected"),
            "identifiers": [{"kind": i["kind"], "value": i["value"], "match": i["match"]["status"],
                             "page": i["source"]["page"]} for i in result["identifiers"]],
            "legibility": result.get("legibility"),
            "missingRequired": next((w["message"] for w in result.get("warnings", [])
                                     if w["code"] == "REQUIRED_MISSING"), None),
            "keyPages": [{"role": k["role"], "page": k["page"]} for k in result.get("keyPages", [])],
            "mismatches": [w["code"] for w in result.get("warnings", []) if w["code"] in obligations.CODES],
            "route": result["document"]["route"],
            "resultUrl": f"/dps/v1/jobs/{result['jobId']}/result"}


def save_run(db: Session, job: Job, result: dict, trigger: str = "SUBMIT") -> Run:
    from dps import results

    jobs.guard_attempt(db)
    results.flag_format(result)  # never saved silently in a shape Spring cannot read (22 Nov 2026)
    last = db.scalar(select(func.max(Run.run_no)).where(Run.job_id == job.id)) or 0
    run = Run(job_id=job.id, run_no=last + 1, status=result["status"], trigger=trigger,
              result_json=json.dumps(result, default=str, separators=(",", ":")))
    if run.run_no > 1:
        result["runId"] = f"{job.id}:{run.run_no}"
        run.result_json = json.dumps(result, default=str, separators=(",", ":"))
    db.add(run)
    db.flush()
    return run


def finish(db: Session, job: Job, result: dict) -> None:
    """Saves the run, emits dps.analysis.completed and ends the job (NEEDS_REVIEW when flagged)."""
    save_run(db, job, result)
    feed.emit_for_job(db, "dps.analysis.completed", job, event_payload(result))
    status = jobs.NEEDS_REVIEW if result["status"] == "NEEDS_REVIEW" else jobs.DONE
    code = None
    flagged = [w["code"] for w in result.get("warnings", [])
               if w["code"] in ("TYPE_DISPUTED", "UNCERTAIN_TYPE", "MAPPING_CONFLICT", *obligations.CODES)]
    if result["classification"]["mismatch"]:
        code = "TYPE_MISMATCH"
    elif result["classification"]["confidence"] < 0.6:
        code = "UNCERTAIN_TYPE"
    elif result["status"] == "NEEDS_REVIEW" and flagged:
        code = flagged[0]
    jobs.finish(db, job, status, None, code)
