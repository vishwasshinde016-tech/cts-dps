"""Production operations (Phase 6): one scheduler leader, the daily audit seal, scaling signals and sizing.

- **Leader lock** (``dps_leader``): every worker runs jobs; only the lock holder runs the scheduler tasks (queue
  aging, outbox, the daily seal). The lock is a row with a lease; a crashed leader's lease runs out and another
  worker takes over. No extra infrastructure.
- **Audit seal** (``dps_audit_seal``): once a day the leader hashes every audit event and review decision of the
  day, in order, chained to the previous day's seal, and writes the seal write-once under ``derived/_seals/``.
  ``verify_seals`` recomputes the chain: any changed, added or removed row of a sealed day shows.
- **Scaling signals**: per pool, the oldest queued job's age and the queue depth per running worker, with the
  scale-out / scale-in advice of the design (autoscaling rules); ``capacity`` is the sizing formula.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dps import jobs
from dps.models import AiCall, AuditEvent, AuditSeal, Job, Leader, ReviewDecision, TraceEvent, utcnow

GENESIS = "0" * 64


# ---------- leader lock ----------

def acquire_leader(db: Session, name: str, holder: str, lease_seconds: int = 60) -> bool:
    """True when ``holder`` holds (or just took) the ``name`` lock for the next ``lease_seconds``."""
    now = utcnow()
    until = now + timedelta(seconds=lease_seconds)
    taken = db.execute(update(Leader).where(Leader.name == name,
                                            (Leader.holder == holder) | (Leader.until <= now))
                       .values(holder=holder, until=until)).rowcount
    if taken:
        db.commit()
        return True
    if db.get(Leader, name) is not None:
        db.rollback()
        return False
    try:
        db.add(Leader(name=name, holder=holder, until=until))
        db.commit()
        return True
    except IntegrityError:  # another worker inserted it first
        db.rollback()
        return False


def release_leader(db: Session, name: str, holder: str) -> None:
    # A second in the past: on a coarse clock "now" can equal the release time, and the next holder must see the
    # lease as ended (this made test_leader_lock_one_holder_and_takeover flaky on Windows).
    db.execute(update(Leader).where(Leader.name == name, Leader.holder == holder)
               .values(until=utcnow() - timedelta(seconds=1)))
    db.commit()


# ---------- audit seal ----------

def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _day_rows(db: Session, day: date) -> list[str]:
    start, end = _day_bounds(day)
    rows = []
    for e in db.scalars(select(AuditEvent).where(AuditEvent.at >= start, AuditEvent.at < end)
                        .order_by(AuditEvent.at, AuditEvent.id)):
        rows.append(json.dumps(["AUDIT", e.id, e.actor, e.action, e.target_type, e.target_id, e.detail_json,
                                e.at.isoformat()], ensure_ascii=False))
    for d in db.scalars(select(ReviewDecision).where(ReviewDecision.at >= start, ReviewDecision.at < end)
                        .order_by(ReviewDecision.at, ReviewDecision.id)):
        rows.append(json.dumps(["REVIEW", d.id, d.job_id, d.run_no, d.field_id, d.reviewer, d.action,
                                d.old_value_json, d.new_value_json, d.reason_code, d.comment, d.at.isoformat()],
                               ensure_ascii=False))
    return rows


def chain(prev: str, rows: list[str]) -> str:
    head = prev
    for row in rows:
        head = hashlib.sha256((head + "\n" + row).encode("utf-8")).hexdigest()
    return head


def seal_day(db: Session, day: date, seals_dir=None) -> AuditSeal:
    """Seals one finished day (idempotent: an existing seal is returned unchanged)."""
    existing = db.get(AuditSeal, day)
    if existing is not None:
        return existing
    prev = db.scalars(select(AuditSeal).where(AuditSeal.day < day).order_by(AuditSeal.day.desc())).first()
    rows = _day_rows(db, day)
    seal = AuditSeal(day=day, rows=len(rows), prev_hash=prev.head_hash if prev else GENESIS,
                     head_hash=chain(prev.head_hash if prev else GENESIS, rows))
    if seals_dir is not None:  # the spec's own audit-seals/ folder, apart from derived/ (separately protected)
        body = json.dumps({"day": day.isoformat(), "rows": seal.rows, "prevHash": seal.prev_hash,
                           "headHash": seal.head_hash}).encode("utf-8")
        folder = Path(seals_dir)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{day.isoformat()}.json"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o440)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        except FileExistsError:  # write-once: an existing seal file is never replaced
            body = target.read_bytes()
        seal.file_sha256 = hashlib.sha256(body).hexdigest()
    db.add(seal)
    db.commit()
    return seal


def seal_pending(db: Session, seals_dir=None, today: date | None = None, max_days: int = 31) -> list[date]:
    """Seals every unsealed finished day since the last seal (at most ``max_days`` per call)."""
    today = today or utcnow().date()
    last = db.scalar(select(func.max(AuditSeal.day)))
    first_event = db.scalar(select(func.min(AuditEvent.at)))
    if last is None and first_event is None:
        return []
    start = (last + timedelta(days=1)) if last else first_event.date()
    sealed = []
    day = start
    while day < today and len(sealed) < max_days:
        seal_day(db, day, seals_dir)
        sealed.append(day)
        day += timedelta(days=1)
    return sealed


def verify_seals(db: Session) -> list[dict]:
    """Recomputes every seal; returns the days whose rows no longer match (empty list: the chain is intact)."""
    problems = []
    prev = GENESIS
    for seal in db.scalars(select(AuditSeal).order_by(AuditSeal.day)):
        rows = _day_rows(db, seal.day)
        head = chain(prev, rows)
        if seal.prev_hash != prev or head != seal.head_hash or len(rows) != seal.rows:
            problems.append({"day": seal.day.isoformat(), "expectedRows": seal.rows, "rows": len(rows)})
        prev = seal.head_hash
    return problems


# ---------- scaling ----------

SCALE_OUT_AGE, SCALE_IN_AGE = 60, 10  # seconds (design: autoscaling rules)
DEPTH_OUT, DEPTH_IN = 5, 1
LIMITS = {"STANDARD": (2, 8), "HEAVY": (1, 4)}


def prune_diagnostics(db: Session, days: int) -> None:
    """Prune one batch of expired diagnostic rows per pass, independently of audit and usage totals."""
    if days <= 0:
        return
    cutoff = utcnow() - timedelta(days=days)
    for model in (AiCall, TraceEvent):
        oldest = select(model.id).where(model.at < cutoff).order_by(model.at, model.id).limit(500)
        db.execute(delete(model).where(model.id.in_(oldest)).execution_options(synchronize_session=False))
    db.commit()


def scaling(db: Session) -> list[dict]:
    now = utcnow()
    out = []
    for pool, (lo, hi) in LIMITS.items():
        ready = (Job.pool == pool, Job.status.in_((jobs.QUEUED, jobs.RETRY_WAIT)), Job.due_at <= now)
        queued, oldest = db.execute(select(func.count(), func.min(Job.due_at)).where(*ready)).one()
        running = db.scalar(select(func.count()).select_from(Job).where(Job.pool == pool,
                                                                         Job.status == jobs.RUNNING)) or 0
        age = (now - oldest).total_seconds() if oldest else 0.0
        depth = queued / max(1, running)
        advice = "HOLD"
        if age > SCALE_OUT_AGE or depth > DEPTH_OUT:
            advice = "SCALE_OUT"
        elif age < SCALE_IN_AGE and depth < DEPTH_IN:
            advice = "SCALE_IN"
        out.append({"pool": pool, "queued": queued, "running": running, "oldestQueuedSeconds": round(age, 1),
                    "depthPerWorker": round(depth, 2), "advice": advice, "minReplicas": lo, "maxReplicas": hi})
    return out


def capacity(docs_per_hour: float, heavy_share: float, light_minutes: float, heavy_minutes: float,
             tokens_per_doc: float, utilisation: float = 0.6) -> dict:
    """workers = documents per hour x share x minutes per document / (60 x utilisation)."""
    light = docs_per_hour * (1 - heavy_share) * light_minutes / (60 * utilisation)
    heavy = docs_per_hour * heavy_share * heavy_minutes / (60 * utilisation)
    tpm = docs_per_hour * tokens_per_doc / 60
    return {"lightWorkers": math.ceil(light - 1e-9), "heavyWorkers": math.ceil(heavy - 1e-9),
            "tokensPerMinute": round(tpm), "askForTpm": int(math.ceil(tpm * 2 / 10_000) * 10_000)}


MAPPING_CODES = ("TYPE_MISMATCH", "TYPE_DISPUTED", "UNCERTAIN_TYPE", "MAPPING_CONFLICT", "ENTITY_MISMATCH")


def review_backlog(db: Session, now: datetime | None = None) -> dict:
    """B4 (spec Flow 4: escalate the mapping-review backlog to operations): jobs waiting for a person, by reason,
    and how long the oldest has waited. Mapping problems are counted apart (route MAPPING_REVIEW)."""
    now = now or utcnow()
    rows = db.execute(select(Job.error_code, func.count(), func.min(Job.finished_at))
                      .where(Job.status == jobs.NEEDS_REVIEW).group_by(Job.error_code)).all()
    by_reason: dict[str, int] = {}
    oldest = None
    mapping = 0
    for code, n, first in rows:
        by_reason[code or "FIELDS_TO_REVIEW"] = by_reason.get(code or "FIELDS_TO_REVIEW", 0) + int(n)
        mapping += int(n) if code in MAPPING_CODES else 0
        if first is not None:
            first = first if first.tzinfo else first.replace(tzinfo=UTC)
            oldest = first if oldest is None or first < oldest else oldest
    return {"waiting": sum(by_reason.values()), "mappingReview": mapping, "byReason": by_reason,
            "oldestWaitSeconds": round((now - oldest).total_seconds()) if oldest is not None else 0}
