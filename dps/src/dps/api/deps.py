"""What every CTS route shares: the database session, the caller, the CP access check and the job view.

The old Covenexa routes (``api/documents.py``: their own upload, term review and publish, spreads, manual values,
learning screens) were deleted in the full review (G12, WP7): in CTS the DMS owns upload and Spring owns approval.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dps.api.access import CpAccessChecker
from dps.api.auth import Principal
from dps.models import Job


def get_db(request: Request):
    yield from request.app.state.db.session()


def get_principal(request: Request) -> Principal:
    principal = request.app.state.authenticator.authenticate(request)
    request.state.user = principal.user  # for the request's line in the log
    return principal


def get_checker(request: Request) -> CpAccessChecker:
    return request.app.state.cp_checker


DB = Annotated[Session, Depends(get_db)]
User = Annotated[Principal, Depends(get_principal)]
Checker = Annotated[CpAccessChecker, Depends(get_checker)]


class ValuesRequest(BaseModel):
    """The bank app's data-source request (``POST /api/data/values``)."""
    cpId: str = Field(min_length=1)  # noqa: N815 - contract field names
    to: date
    keys: list[str]
    from_: date | None = Field(default=None, alias="from")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


_PRIORITY_NAME = {1: "INTERACTIVE", 2: "AMENDMENT", 3: "ASSESSMENT", 4: "BULK", 5: "REPROCESS"}


def job_view(j: Job) -> dict:
    """The job status (contracts/schemas/job-status.json)."""
    return {
        "id": j.id, "documentId": j.document_id, "flow": j.flow, "status": j.status, "pool": j.pool,
        "attempts": j.attempts, "currentStep": j.current_step, "error": j.error,
        "steps": json.loads(j.steps_json or "[]"), "createdAt": j.created_at.isoformat(),
        "updatedAt": _iso(j.updated_at),
        "jobType": j.job_type, "priority": _PRIORITY_NAME.get(j.priority, "INTERACTIVE"),
        "tenantId": j.tenant_id, "documentVersionId": j.document_version_id, "batchId": j.batch_id,
        "correlationId": j.correlation_id, "stage": j.current_step, "cancelRequested": bool(j.cancel_requested),
        "errorCode": j.error_code, "startedAt": _iso(j.started_at), "finishedAt": _iso(j.finished_at),
        "dueAt": _iso(j.due_at),
        "resultUrl": f"/dps/v1/jobs/{j.id}/result" if j.status in ("DONE", "NEEDS_REVIEW") else None,
    }
