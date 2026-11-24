"""Event feed for the CTS Spring app (contracts/catalogues/events.yaml, schemas/event.json).

Events are rows in ``dps_event_feed``, added in the same transaction as the change they report, so a change is
never saved without its event or announced without being saved. Readers page with ``after=<seq>``; delivery is
at least once and consumers de-duplicate on ``eventId``. Payloads never hold document content.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps.models import EventFeed, Job

SCHEMA = "dps.event/1.0"
PAYLOAD_VERSION = "1.0"

# Job status -> the event a finished job emits.
FINAL_EVENTS = {
    "DONE": "dps.extraction.completed",
    "NEEDS_REVIEW": "dps.extraction.needs_review",
    "FAILED": "dps.job.failed",
    "DEAD": "dps.job.dead",
    "CANCELLED": "dps.job.cancelled",
}


def _db_now(db: Session):
    """The database's own UTC clock, as a SQL expression: writers on several hosts and the reader all use one clock,
    so a host whose clock runs fast or slow cannot make the reader skip an event (CTS review, 23 Nov 2026)."""
    from sqlalchemy import func

    dialect = db.get_bind().dialect.name
    return func.sysutcdatetime() if dialect == "mssql" else func.current_timestamp()


def emit(db: Session, type_: str, *, tenant_id: str = "default", job_id: str | None = None,
         document_version_id: str | None = None, correlation_id: str | None = None,
         payload: dict | None = None) -> EventFeed:
    """Adds an event to the session; the caller's commit publishes it together with the change."""
    body = {**(payload or {}), "schemaVersion": PAYLOAD_VERSION}
    event = EventFeed(type=type_, tenant_id=tenant_id, job_id=job_id, document_version_id=document_version_id,
                      correlation_id=correlation_id, payload_json=json.dumps(body, default=str),
                      occurred_at=_db_now(db))
    db.add(event)
    return event


def emit_for_job(db: Session, type_: str, job: Job, payload: dict | None = None) -> EventFeed:
    body = dict(payload or {})
    if job.pipeline_version:
        body.setdefault("pipelineVersion", job.pipeline_version)
    return emit(db, type_, tenant_id=job.tenant_id or "default", job_id=job.id,
                document_version_id=job.document_version_id, correlation_id=job.correlation_id, payload=body)


def result_metadata(result: dict) -> dict:
    """Use the public result's run identity and the pipeline that wrote that result."""
    return {key: value for key, value in (("runId", result.get("runId")),
                                         ("pipelineVersion", (result.get("pipeline") or {}).get("version"))) if value}


def view(event: EventFeed) -> dict:
    return {
        "eventId": event.event_id, "seq": event.seq, "type": event.type,
        "occurredAt": event.occurred_at.isoformat(), "tenantId": event.tenant_id,
        "correlationId": event.correlation_id, "jobId": event.job_id,
        "documentVersionId": event.document_version_id, "schema": SCHEMA,
        "payload": json.loads(event.payload_json or "{}"),
    }


# A missing ``seq`` younger than this may be an event whose transaction has not committed yet (SQL Server hands out
# IDENTITY values at insert, transactions commit in any order). Older gaps are rollbacks or identity-cache jumps.
GAP_SETTLE_SECONDS = 300  # 5 minutes: longer than any DPS transaction that writes an event


def _now(db: Session) -> datetime:
    value = db.scalar(select(_db_now(db)))
    if isinstance(value, str):  # SQLite answers text
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def read(db: Session, after: int, limit: int, tenant_id: str | None = None,
         now: datetime | None = None) -> tuple[list[dict], int]:
    """Events with ``seq > after``, oldest first, and the cursor to pass next time.

    The page stops before a recent gap in ``seq`` (full review G24): a reader that moved its cursor past a sequence
    number still being written would never see that event. Once the gap is older than ``GAP_SETTLE_SECONDS`` (both
    times from the database's clock) it is a rollback, not a pending write, and reading goes on past it."""
    now = now or _now(db)
    size = max(1, min(limit, 500))
    rows = list(db.scalars(select(EventFeed).where(EventFeed.seq > after).order_by(EventFeed.seq).limit(size)))
    kept, expected = [], after + 1
    for e in rows:
        if e.seq != expected:
            at = e.occurred_at if e.occurred_at.tzinfo else e.occurred_at.replace(tzinfo=UTC)
            if (now - at).total_seconds() < GAP_SETTLE_SECONDS:
                break  # an earlier number may still commit: wait for it
        kept.append(e)
        expected = e.seq + 1
    cursor = kept[-1].seq if kept else after
    if tenant_id is not None:
        kept = [e for e in kept if e.tenant_id == tenant_id]
    return [view(e) for e in kept], cursor
