"""Job queue on the ``dps_job`` table (03-flows-design.md §4; CTS DPS design, Phase 1 tab).

Claiming is a conditional UPDATE (compare-and-set on status + attempts), so it is safe with many workers on
SQL Server and on SQLite alike. A RUNNING job whose lease expired (worker died) is claimable again, until it
has killed its worker ``poison_crashes`` times: then it is DEAD, so one bad file cannot block the queue.

Phase 1 adds: priorities (1 INTERACTIVE ... 5 REPROCESS) with aging, per-tenant and per-batch running limits,
a retry wait that grows (RETRY_WAIT + due_at), the DEAD state, cancel, an attempt history and the event feed.
"""

from __future__ import annotations

import json
import random
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import false, func, or_, select, update
from sqlalchemy.orm import Session

from dps import feed
from dps.models import Batch, Job, JobAttempt, TenantLimit, utcnow

QUEUED, RUNNING, NEEDS_REVIEW, DONE, FAILED = "QUEUED", "RUNNING", "NEEDS_REVIEW", "DONE", "FAILED"
RETRY_WAIT, DEAD, CANCELLED = "RETRY_WAIT", "DEAD", "CANCELLED"
WAITING = (QUEUED, RETRY_WAIT)
FINAL = (DONE, FAILED, DEAD, CANCELLED)

PRIORITY = {"INTERACTIVE": 1, "AMENDMENT": 2, "ASSESSMENT": 3, "BULK": 4, "REPROCESS": 5}
PRIORITY_NAME = {v: k for k, v in PRIORITY.items()}
CANDIDATES = 20


@dataclass(frozen=True)
class Limits:
    """Running limits; ``None`` turns a limit off (the older single-tenant path)."""

    per_tenant: int | None = None
    per_batch: int | None = None


class JobFinished(Exception):
    """The job already ended; it cannot be cancelled."""


class LeaseLost(Exception):
    """A stale worker attempt may no longer publish results."""


worker_attempt: ContextVar[tuple[str, str, int] | None] = ContextVar("worker_attempt", default=None)


def guard_attempt(db: Session) -> None:
    """Hold the claim row until the transaction publishing this attempt commits.

    The attempt still owns the job while no other worker has claimed it: a claim changes ``locked_by`` and
    ``attempts``. A lease that ran out (a late heartbeat during a long OCR) does not throw finished work away."""
    attempt = worker_attempt.get()
    if attempt is None:
        return
    job_id, worker_id, number = attempt
    with db.no_autoflush:
        result = db.execute(
            update(Job).where(Job.id == job_id, Job.status == RUNNING, Job.locked_by == worker_id,
                              Job.attempts == number)
            .values(updated_at=utcnow()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLost(job_id)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


def _running_counts(db: Session, now: datetime, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).where(Job.status == RUNNING, Job.locked_until >= now,
                                                         column.is_not(None)).group_by(column)).all()
    return {key: count for key, count in rows}


def claim(db: Session, worker_id: str, pool: str, lease_seconds: int, max_attempts: int,
          limits: Limits | None = None, poison_crashes: int = 2) -> Job | None:
    """Claims the most urgent runnable job of ``pool`` for ``worker_id`` or returns None.

    Order: priority, then due time, then age. Jobs whose tenant or batch is at its running limit are skipped,
    so one bulk import cannot hold every worker while an RM's single upload waits.
    """
    now = utcnow()
    candidates = _candidates(db, pool, max_attempts, now)
    limits = limits or Limits()
    tenants = _running_counts(db, now, Job.tenant_id) if limits.per_tenant else {}
    batches = _running_counts(db, now, Job.batch_id) if limits.per_batch else {}
    for job_id in candidates:
        current = db.get(Job, job_id)
        if current is None:
            continue
        if limits.per_tenant and tenants.get(current.tenant_id, 0) >= limits.per_tenant:
            continue
        if limits.per_batch and current.batch_id and batches.get(current.batch_id, 0) >= limits.per_batch:
            continue
        if current.status == RUNNING and current.locked_until is not None and current.locked_until >= now:
            continue  # another worker claimed it after the candidate list was read: its lease is still valid
        crashed = current.status == RUNNING  # its lease expired: the worker holding it died
        crashes = current.crash_count + (1 if crashed else 0)
        if crashed and crashes >= poison_crashes:
            _bury_poison(db, current, crashes)
            continue
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == current.status, Job.attempts == current.attempts,
                   # never take a running job whose lease is still valid (e2e 17 Nov: two workers, one job)
                   or_(Job.status != RUNNING, Job.locked_until < now))
            .values(status=RUNNING, locked_by=worker_id, locked_until=now + timedelta(seconds=lease_seconds),
                    attempts=Job.attempts + 1, crash_count=crashes, updated_at=now,
                    started_at=func.coalesce(Job.started_at, now))
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.rollback()
            continue  # another worker won it
        db.add(JobAttempt(job_id=job_id, attempt_no=current.attempts + 1, worker_id=worker_id, started_at=now))
        db.commit()
        job = db.get(Job, job_id)
        db.refresh(job)
        return job
    return None


def _candidates(db: Session, pool: str, max_attempts: int, now) -> list[str]:
    """Runnable job ids, most urgent first. The list can be stale by the time a claim is tried."""
    return list(db.scalars(
        select(Job.id)
        .where(Job.pool == pool, Job.attempts < max_attempts, Job.cancel_requested == false(),
               or_(Job.status.in_(WAITING) & (Job.due_at.is_(None) | (Job.due_at <= now)),
                   (Job.status == RUNNING) & (Job.locked_until < now)))
        .order_by(Job.priority, Job.due_at, Job.created_at)
        .limit(CANDIDATES)).all())


def _bury_poison(db: Session, job: Job, crashes: int) -> None:
    """A file that killed its worker ``crashes`` times: DEAD, never claimed again, ops alerted by the event."""
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == RUNNING, Job.attempts == job.attempts)
        .values(status=DEAD, crash_count=crashes, error_code="PARSER_CRASH", locked_by=None, locked_until=None,
                error="The worker stopped while processing this file, twice; it was set aside.",
                finished_at=now, updated_at=now)
        .execution_options(synchronize_session=False))
    if result.rowcount == 1:
        db.refresh(job)
        _close_attempt(db, job, DEAD, "PARSER_CRASH")
        feed.emit_for_job(db, "dps.job.dead", job,
                          {"errorCode": "PARSER_CRASH", "message": job.error, "attempts": job.attempts})
        _sync_batch(db, job)
    db.commit()


def renew_lease(db: Session, job_id: str, worker_id: str, lease_seconds: int,
                attempt: int | None = None) -> bool:
    """Heartbeat: pushes the lease out while ``worker_id`` still holds the running job. False if it lost it.

    A late beat after the lease ran out still renews it when no other worker has claimed the job since."""
    now = utcnow()
    claim = update(Job).where(Job.id == job_id, Job.status == RUNNING, Job.locked_by == worker_id)
    if attempt is not None:
        claim = claim.where(Job.attempts == attempt)
    result = db.execute(
        claim
        .values(locked_until=now + timedelta(seconds=lease_seconds), updated_at=now)
        .execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount == 1


def record_step(db: Session, job: Job, step: str, **detail) -> None:
    guard_attempt(db)
    steps = json.loads(job.steps_json or "[]")
    steps.append({"step": step, "at": utcnow().isoformat(), **detail})
    job.steps_json, job.current_step, job.updated_at = json.dumps(steps, default=str), step, utcnow()
    db.commit()


def _close_attempt(db: Session, job: Job, outcome: str, error_code: str | None) -> None:
    attempt = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.ended_at.is_(None))
                         .order_by(JobAttempt.attempt_no.desc())).first()
    if attempt is not None:
        attempt.ended_at, attempt.outcome, attempt.error_code = utcnow(), outcome, error_code


def finish(db: Session, job: Job, status: str, error: str | None = None, error_code: str | None = None,
           payload: dict | None = None) -> None:
    """Ends the job's current attempt with ``status``; a final status also emits its event."""
    guard_attempt(db)
    now = utcnow()
    job.status, job.error, job.locked_by, job.locked_until, job.updated_at = status, error, None, None, now
    if error_code is not None or status in (DONE, NEEDS_REVIEW):
        job.error_code = error_code
    _close_attempt(db, job, status, job.error_code)
    event = feed.FINAL_EVENTS.get(status)
    if job.job_type == "ANALYZE" and status in (DONE, NEEDS_REVIEW):
        event = None  # ANALYZE reports with dps.analysis.completed (dps.analyze.analysis.finish)
    elif getattr(job, "rollout", "NORMAL") == "SHADOW" and status in (DONE, NEEDS_REVIEW):
        event = "dps.extraction.shadow"  # phase 7 pilot: stored for comparison, not shown to analysts
    if status in FINAL or status == NEEDS_REVIEW:
        job.finished_at = now
    if event:
        body = dict(payload or {})
        if status in (DONE, NEEDS_REVIEW):
            body.setdefault("resultUrl", f"/dps/v1/jobs/{job.id}/result")
        elif error is not None or job.error_code:
            body.setdefault("errorCode", job.error_code or "INTERNAL_ERROR")
            body.setdefault("message", (error or "")[:500])
            if status == DEAD:
                body.setdefault("attempts", job.attempts)
        feed.emit_for_job(db, event, job, body)
    _sync_batch(db, job)
    db.commit()


def retry_delay(attempt: int, base: float, maximum: float) -> float:
    """``min(maximum, base * 4^(attempt-1))`` seconds, +/-20% so retries of one outage spread out."""
    delay = min(maximum, base * (4 ** max(0, attempt - 1)))
    return delay * random.uniform(0.8, 1.2)  # retry jitter, not security  # noqa: S311  # nosec B311


def release_for_retry(db: Session, job: Job, error: str, max_attempts: int, error_code: str = "INTERNAL_ERROR",
                      base_seconds: float = 10.0, max_seconds: float = 900.0) -> None:
    """Transient failure: RETRY_WAIT until ``due_at``; when attempts run out, DEAD (ops alerted)."""
    if job.attempts >= max_attempts:
        finish(db, job, DEAD, error, error_code)
        return
    guard_attempt(db)
    now = utcnow()
    job.status, job.error, job.error_code = RETRY_WAIT, error, error_code
    job.locked_by, job.locked_until, job.updated_at = None, None, now
    job.due_at = now + timedelta(seconds=retry_delay(job.attempts, base_seconds, max_seconds))
    _close_attempt(db, job, RETRY_WAIT, error_code)
    db.commit()


def move_to_pool(db: Session, job: Job, pool: str) -> None:
    """Hands the job to another pool (fast parse not good enough → HEAVY) without using up an attempt."""
    guard_attempt(db)
    job.pool, job.status = pool, QUEUED
    job.attempts = max(0, job.attempts - 1)
    job.locked_by, job.locked_until, job.updated_at = None, None, utcnow()
    _close_attempt(db, job, "MOVED_" + pool, None)
    db.commit()


def request_cancel(db: Session, job: Job, user: str) -> Job:
    """A waiting job is cancelled at once; a running one is flagged and stops at its next checkpoint."""
    if job.status in FINAL:
        raise JobFinished(job.status)
    if job.status == RUNNING:
        job.cancel_requested, job.updated_at = True, utcnow()
        db.commit()
        return job
    job.cancel_requested = True
    finish(db, job, CANCELLED, f"Cancelled by {user}", "JOB_CANCELLED", {"cancelledBy": user})
    return job


def cancelled(db: Session, job_id: str) -> bool:
    """For a running flow to check between stages."""
    return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def age(db: Session, minutes: int) -> int:
    """Moves waiting jobs one priority step up per ``minutes`` waited (not past 1); returns how many moved."""
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(Job.status.in_(WAITING), Job.priority > 1,
               func.coalesce(Job.updated_at, Job.created_at) <= now - timedelta(minutes=minutes))
        .values(priority=Job.priority - 1, updated_at=now)
        .execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount or 0


def tenant_limits(db: Session, tenant_id: str, defaults: TenantLimit) -> TenantLimit:
    return db.get(TenantLimit, tenant_id) or defaults


def waiting_count(db: Session, tenant_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Job)
                     .where(Job.tenant_id == tenant_id, Job.status.in_(WAITING))) or 0


def queue_report(db: Session) -> list[dict]:
    """Depth and oldest wait per pool, status and priority (for ops and alerts)."""
    now = utcnow()
    rows = db.execute(select(Job.pool, Job.status, Job.priority, func.count(), func.min(Job.created_at))
                      .where(Job.status.in_((*WAITING, RUNNING)))
                      .group_by(Job.pool, Job.status, Job.priority)
                      .order_by(Job.pool, Job.status, Job.priority)).all()
    return [{"pool": pool, "status": status, "priority": priority, "count": count,
             "oldestSeconds": round((now - _aware(oldest)).total_seconds(), 1) if oldest else None}
            for pool, status, priority, count, oldest in rows]


# ---------- batches ----------

BATCH_OUTCOME = {DONE: "SUCCEEDED", NEEDS_REVIEW: "PENDING_MAPPING", FAILED: "REJECTED", DEAD: "FAILED_RETRYABLE",
                 CANCELLED: "REJECTED"}


def item_outcome(job: Job) -> str:
    if job.error_code == "DUPLICATE":
        return "DUPLICATE"
    return BATCH_OUTCOME.get(job.status, "RUNNING")


def _sync_batch(db: Session, job: Job) -> None:
    """When the last item of a batch ends, the batch completes and says so on the feed."""
    if not job.batch_id:
        return
    db.flush()
    open_items = db.scalar(select(func.count()).select_from(Job)
                           .where(Job.batch_id == job.batch_id, Job.status.not_in(FINAL + (NEEDS_REVIEW,))))
    batch = db.get(Batch, job.batch_id)
    if batch is None or open_items or batch.status == "COMPLETED":
        return
    batch.status, batch.finished_at = "COMPLETED", utcnow()
    counts: dict[str, int] = {}
    for item in db.scalars(select(Job).where(Job.batch_id == batch.id)):
        outcome = item_outcome(item)
        counts[outcome] = counts.get(outcome, 0) + 1
    failed = any(k in counts for k in ("REJECTED", "FAILED_RETRYABLE"))
    feed.emit(db, "dps.batch.completed", tenant_id=batch.tenant_id, payload={
        "batchId": batch.id, "counts": counts,
        "errorsUrl": f"/dps/v1/batches/{batch.id}/errors.csv" if failed else None})


def lease_expired(job: Job) -> bool:
    until = _aware(job.locked_until)
    return until is None or until < utcnow()
