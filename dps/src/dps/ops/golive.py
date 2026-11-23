"""Go-live readiness per job type (Phase 7): the measured numbers against the targets of the go-live checklist.

A document type goes live only when every target is met; until then it stays in SHADOW (``rollout_modes``).
Classification accuracy and calibration error come from the offline golden-set runs and are passed in; the rest is
measured from the live tables: grounding rate of stored values, the 3% QA sample's error rate, p95 time.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from dps.models import Job, ReviewDecision, Run, utcnow

TARGETS = {"classificationAccuracy": 0.98, "groundingRate": 1.0, "sampledErrorRate": 0.01,
           "calibrationError": 0.05, "p95SecondsNative": 300, "p95SecondsScan": 900}
EXTRACTION_TYPES = ("EXTRACT_AGREEMENT", "EXTRACT_STATEMENT", "EXTRACT_SANCTION", "EXTRACT_CERTIFICATE",
                    "EXTRACT_AMENDMENT", "EXTRACT_WAIVER", "AUTO")


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(0.95 * len(values)))], 1)


def report(db: Session, days: int = 30, offline: dict | None = None) -> dict:
    """``offline``: {jobType: {"classificationAccuracy": x, "calibrationError": y}} from the golden-set runs."""
    since = utcnow() - timedelta(days=days)
    offline = offline or {}
    out = []
    for job_type in EXTRACTION_TYPES:
        jobs_ = db.scalars(select(Job).where(Job.job_type == job_type, Job.created_at >= since)).all()
        times = {"HEAVY": [], "STANDARD": []}
        for j in jobs_:
            if j.started_at and j.finished_at:
                times["HEAVY" if j.pool == "HEAVY" else "STANDARD"].append(
                    (j.finished_at - j.started_at).total_seconds())
        stored = grounded = 0
        ids = [j.id for j in jobs_]
        if ids:
            for run in db.scalars(select(Run).where(Run.job_id.in_(ids))):
                for f in json.loads(run.result_json).get("fields", []):
                    if f.get("status") == "REJECTED" or f.get("method") == "MANUAL":
                        continue
                    stored += 1
                    src = f.get("source") or {}
                    grounded += bool(src.get("page") and (src.get("quote") or src.get("cellRef")))
        sampled = wrong = 0
        if ids:
            for d in db.scalars(select(ReviewDecision).where(ReviewDecision.job_id.in_(ids),
                                                             ReviewDecision.sampled == true())):
                sampled += 1
                wrong += d.action in ("CORRECT", "REJECT")
        measured = {
            "jobs": len(jobs_),
            "groundingRate": round(grounded / stored, 4) if stored else None,
            "sampledErrorRate": round(wrong / sampled, 4) if sampled else None,
            "sampledFields": sampled,
            "p95SecondsNative": _p95(times["STANDARD"]),
            "p95SecondsScan": _p95(times["HEAVY"]),
            "classificationAccuracy": offline.get(job_type, {}).get("classificationAccuracy"),
            "calibrationError": offline.get(job_type, {}).get("calibrationError"),
        }
        checks = {
            "classificationAccuracy": _at_least(measured["classificationAccuracy"], TARGETS["classificationAccuracy"]),
            "groundingRate": _at_least(measured["groundingRate"], TARGETS["groundingRate"]),
            "sampledErrorRate": _at_most(measured["sampledErrorRate"], TARGETS["sampledErrorRate"]),
            "calibrationError": _at_most(measured["calibrationError"], TARGETS["calibrationError"]),
            "p95SecondsNative": _at_most(measured["p95SecondsNative"], TARGETS["p95SecondsNative"]),
            "p95SecondsScan": _at_most(measured["p95SecondsScan"], TARGETS["p95SecondsScan"], optional=True),
        }
        ready = all(v in ("PASS", "N/A") for v in checks.values())
        out.append({"jobType": job_type, "measured": measured, "checks": checks, "readyToGoLive": ready})
    return {"days": days, "targets": TARGETS, "types": out}


def _at_least(value, target) -> str:
    if value is None:
        return "NO_DATA"
    return "PASS" if value >= target else "FAIL"


def _at_most(value, target, optional: bool = False) -> str:
    if value is None:
        return "N/A" if optional else "NO_DATA"
    return "PASS" if value <= target else "FAIL"
