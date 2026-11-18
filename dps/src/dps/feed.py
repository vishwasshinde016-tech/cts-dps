"""Event feed for the CTS Spring app (contracts/catalogues/events.yaml, schemas/event.json).

Events are rows in ``dps_event_feed``, added in the same transaction as the change they report, so a change is
never saved without its event or announced without being saved. Readers page with ``after=<seq>``; delivery is
at least once and consumers de-duplicate on ``eventId``. Payloads never hold document content.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps.models import EventFeed, Job

SCHEMA = "dps.event/1.0"

# Job status -> the event a finished job emits.
FINAL_EVENTS = {
    "DONE": "dps.extraction.completed",
    "NEEDS_REVIEW": "dps.extraction.needs_review",
    "FAILED": "dps.job.failed",
    "DEAD": "dps.job.dead",
    "CANCELLED": "dps.job.cancelled",
}


def emit(db: Session, type_: str, *, tenant_id: str = "default", job_id: str | None = None,
         document_version_id: str | None = None, correlation_id: str | None = None,
         payload: dict | None = None) -> EventFeed:
    """Adds an event to the session; the caller's commit publishes it together with the change."""
    event = EventFeed(type=type_, tenant_id=tenant_id, job_id=job_id, document_version_id=document_version_id,
                      correlation_id=correlation_id, payload_json=json.dumps(payload or {}, default=str))
    db.add(event)
    return event


def emit_for_job(db: Session, type_: str, job: Job, payload: dict | None = None) -> EventFeed:
    return emit(db, type_, tenant_id=job.tenant_id or "default", job_id=job.id,
                document_version_id=job.document_version_id, correlation_id=job.correlation_id, payload=payload)


def view(event: EventFeed) -> dict:
    return {
        "eventId": event.event_id, "seq": event.seq, "type": event.type,
        "occurredAt": event.occurred_at.isoformat(), "tenantId": event.tenant_id,
        "correlationId": event.correlation_id, "jobId": event.job_id,
        "documentVersionId": event.document_version_id, "schema": SCHEMA,
        "payload": json.loads(event.payload_json or "{}"),
    }


def read(db: Session, after: int, limit: int, tenant_id: str | None = None) -> tuple[list[dict], int]:
    """Events with ``seq > after``, oldest first, and the cursor to pass next time."""
    q = select(EventFeed).where(EventFeed.seq > after).order_by(EventFeed.seq).limit(max(1, min(limit, 500)))
    if tenant_id is not None:
        q = q.where(EventFeed.tenant_id == tenant_id)
    events = list(db.scalars(q))
    return [view(e) for e in events], (events[-1].seq if events else after)
