"""Database tables (schema ``dps_*``). Owned by DPS; Core never writes here."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Unicode,
    UnicodeText,
    cast,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from dps.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """Timestamp stored in UTC and always read back timezone-aware.

    SQLite drops the offset even for ``DateTime(timezone=True)``; a naive value serialised for Core
    (java.time.Instant) or the UI would then be unparseable or read as local time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return value.astimezone(UTC) if value is not None and value.tzinfo else value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None or value.tzinfo:
            return value
        return value.replace(tzinfo=UTC)


class Varchar(TypeDecorator):
    """A VARCHAR column whose bound values are sent as VARCHAR too.

    pymssql sends every Python str as NVARCHAR (``N'...'``). Against a VARCHAR column SQL Server then
    converts each row instead of seeking the index, so two uploads lock each other's rows and deadlock
    (review BUG-04: 22-70% of concurrent uploads failed with error 1205). Casting the parameter keeps the
    comparison VARCHAR = VARCHAR. On SQLite the cast is a no-op.
    """

    impl = String
    cache_ok = True

    def bind_expression(self, bindvalue):
        return cast(bindvalue, String(self.impl.length))


# Text read from documents or typed by people (quotes, labels, notes, file names) is NVARCHAR: a VARCHAR column
# on SQL Server (code page 1252) stored "₹" as "?" and "≥" as "=". pymssql already sends str as NVARCHAR, so
# these Unicode columns need no cast.


class Document(Base):
    __tablename__ = "dps_document"

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(Varchar(32))  # LOAN_AGREEMENT | FINANCIAL_STATEMENT | EVIDENCE
    file_name: Mapped[str] = mapped_column(Unicode(255))
    file_id: Mapped[str | None] = mapped_column(Varchar(255))  # external file service id (later)
    storage_path: Mapped[str] = mapped_column(Varchar(500))
    sha256: Mapped[str] = mapped_column(Varchar(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    as_of_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Varchar(24), default="QUEUED")
    uploaded_by: Mapped[str] = mapped_column(Varchar(255))
    uploaded_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    latest_job_id: Mapped[str | None] = mapped_column(Varchar(36))
    # C7: set when this agreement amends an earlier one (its terms replace the earlier terms).
    amends_document_id: Mapped[str | None] = mapped_column(Varchar(36))
    # Phase 1 (job API): the tenant (one default today: the bank app has none), the DMS document_version_id and
    # the key of the accepted bytes in the DMS store. Null for documents uploaded through the older upload route.
    tenant_id: Mapped[str] = mapped_column(Varchar(64), default="default", server_default="default")
    external_version_id: Mapped[str | None] = mapped_column(Varchar(64), index=True)
    object_key: Mapped[str | None] = mapped_column(Varchar(500))
    # Flow B: {"issues": [...], "unitScale": "lakh"}, so the reviewer sees why a statement waits and in what unit
    # it was printed.
    review_json: Mapped[str | None] = mapped_column(Text)

    links: Mapped[list[DocumentCpLink]] = relationship(back_populates="document", cascade="all, delete-orphan")

    def active_cp_ids(self) -> list[str]:
        return sorted({lk.cp_id for lk in self.links if lk.unlinked_at is None})


class DocumentCpLink(Base):
    """One document can serve several CPs (decision D7)."""

    __tablename__ = "dps_document_cp_link"
    __table_args__ = (Index("ix_dps_link_cp", "cp_id"),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("dps_document.id"))
    cp_id: Mapped[str] = mapped_column(Varchar(64))
    linked_by: Mapped[str] = mapped_column(Varchar(255))
    linked_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    unlinked_by: Mapped[str | None] = mapped_column(Varchar(255))
    unlinked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    document: Mapped[Document] = relationship(back_populates="links")


class Job(Base):
    __tablename__ = "dps_job"
    __table_args__ = (
        # Phase 1: the claim reads through this index (pool, status, priority, due) and never scans the table.
        Index("ix_dps_job_claim", "pool", "status", "priority", "due_at"),
        # Phase 1: the same Idempotency-Key from the same tenant always means the same job.
        Index("ux_dps_job_idempotency", "tenant_id", "idempotency_key", unique=True,
              mssql_where=text("idempotency_key IS NOT NULL"), sqlite_where=text("idempotency_key IS NOT NULL")),
        Index("ux_dps_job_scoped_input", "tenant_id", "input_key", unique=True,
              mssql_where=text("input_key IS NOT NULL"), sqlite_where=text("input_key IS NOT NULL")),
    )

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("dps_document.id"))
    # SKILL_PACK | ANALYZE
    flow: Mapped[str] = mapped_column(Varchar(32))
    # QUEUED | RUNNING | RETRY_WAIT | NEEDS_REVIEW | DONE | FAILED | DEAD | CANCELLED
    status: Mapped[str] = mapped_column(Varchar(16), default="QUEUED")
    pool: Mapped[str] = mapped_column(Varchar(16), default="STANDARD")  # STANDARD | HEAVY
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[str | None] = mapped_column(Varchar(32))
    error: Mapped[str | None] = mapped_column(Text)
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    locked_by: Mapped[str | None] = mapped_column(Varchar(64))
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    # ---- Phase 1: job API and queue upgrades (contracts/dps-jobs-api.yaml) ----
    job_type: Mapped[str] = mapped_column(Varchar(32), default="EXTRACT_AGREEMENT",
                                          server_default="EXTRACT_AGREEMENT")
    tenant_id: Mapped[str] = mapped_column(Varchar(64), default="default", server_default="default")
    priority: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # 1 INTERACTIVE ... 5 REPROCESS
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=utcnow)  # not claimed before this time
    idempotency_key: Mapped[str | None] = mapped_column(Varchar(128))
    request_hash: Mapped[str | None] = mapped_column(Varchar(64))  # SHA-256 of the submitted body
    input_key: Mapped[str | None] = mapped_column(Varchar(64))
    request_json: Mapped[str | None] = mapped_column(UnicodeText)
    batch_id: Mapped[str | None] = mapped_column(Varchar(36), index=True)
    client_file_reference: Mapped[str | None] = mapped_column(Unicode(128))
    correlation_id: Mapped[str | None] = mapped_column(Varchar(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    error_code: Mapped[str | None] = mapped_column(Varchar(48))
    pipeline_version: Mapped[str] = mapped_column(Varchar(32), default="1", server_default="1")
    # Phase 7: the rollout mode this job ran under (SHADOW | ASSISTED | NORMAL), from settings at submit time.
    rollout: Mapped[str] = mapped_column(Varchar(16), default="NORMAL", server_default="NORMAL")
    document_version_id: Mapped[str | None] = mapped_column(Varchar(64))
    sha256: Mapped[str | None] = mapped_column(Varchar(64))
    crash_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # leases that expired
    requested_by: Mapped[str | None] = mapped_column(Varchar(255))


class Batch(Base):
    """Phase 1: one bulk submission (flow 4); its items are jobs with ``batch_id`` set."""

    __tablename__ = "dps_batch"
    __table_args__ = (Index("ux_dps_batch_idempotency", "tenant_id", "idempotency_key", unique=True),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(Varchar(64), default="default")
    batch_ref: Mapped[str] = mapped_column(Unicode(128))
    idempotency_key: Mapped[str] = mapped_column(Varchar(128))
    request_hash: Mapped[str] = mapped_column(Varchar(64))
    status: Mapped[str] = mapped_column(Varchar(16), default="RUNNING")  # RUNNING | COMPLETED
    item_count: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(Varchar(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class JobAttempt(Base):
    """Phase 1: one row per attempt, so support sees the whole retry history of a job."""

    __tablename__ = "dps_job_attempt"

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(Varchar(36), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(Varchar(64))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    outcome: Mapped[str | None] = mapped_column(Varchar(24))
    error_code: Mapped[str | None] = mapped_column(Varchar(48))


class TenantLimit(Base):
    """Phase 1: per-tenant queue limits; settings give the defaults when a tenant has no row."""

    __tablename__ = "dps_tenant_limit"

    tenant_id: Mapped[str] = mapped_column(Varchar(64), primary_key=True)
    max_queued: Mapped[int] = mapped_column(Integer)
    max_running: Mapped[int] = mapped_column(Integer)
    max_running_per_batch: Mapped[int] = mapped_column(Integer)


class EventFeed(Base):
    """Phase 1: the event feed Spring reads with GET /dps/v1/events (contracts/catalogues/events.yaml).

    The only event channel (full review G9): the old ``dps_event_outbox`` table is no longer written.
    ``seq`` is the cursor; it only grows.
    """

    __tablename__ = "dps_event_feed"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Varchar(36), unique=True, default=new_id)
    type: Mapped[str] = mapped_column(Varchar(48))
    tenant_id: Mapped[str] = mapped_column(Varchar(64), default="default")
    job_id: Mapped[str | None] = mapped_column(Varchar(36))
    document_version_id: Mapped[str | None] = mapped_column(Varchar(64))
    correlation_id: Mapped[str | None] = mapped_column(Varchar(128))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    payload_json: Mapped[str] = mapped_column(UnicodeText, default="{}")


class Artifact(Base):
    """Phase 2: every derived file (page model, page image, raw OCR), write-once under derived/.

    ``sha256`` of the source file lets a page model built for one version be reused by every job on the same bytes.
    """

    __tablename__ = "dps_artifact"
    __table_args__ = (Index("ux_dps_artifact_path", "path", unique=True),
                      Index("ix_dps_artifact_source", "source_sha256", "kind", "pipeline_version"))

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(Varchar(64), default="default")
    version_id: Mapped[str] = mapped_column(Varchar(64))
    kind: Mapped[str] = mapped_column(Varchar(24))  # PAGE_MODEL | PAGE_IMAGE | OCR | RESULT
    path: Mapped[str] = mapped_column(Varchar(400))
    sha256: Mapped[str] = mapped_column(Varchar(64))
    bytes: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str | None] = mapped_column(Varchar(64))
    pipeline_version: Mapped[str | None] = mapped_column(Varchar(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Run(Base):
    """Phase 3: one result per run of a job (dps.result/1.0 JSON). ANALYZE stores its analysis here; from phase 4
    every extraction run does, and GET /dps/v1/jobs/{id}/runs lists them."""

    __tablename__ = "dps_run"
    __table_args__ = (Index("ux_dps_run_job_no", "job_id", "run_no", unique=True),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(Varchar(36))
    run_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Varchar(16))  # DONE | NEEDS_REVIEW | FAILED
    trigger: Mapped[str] = mapped_column(Varchar(24), default="SUBMIT")  # SUBMIT | REPROCESS | REVIEW
    result_json: Mapped[str] = mapped_column(UnicodeText)
    superseded_by: Mapped[str | None] = mapped_column(Varchar(64))  # phase 5: the run (or job) that replaced it
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class ReviewDecision(Base):
    """Phase 5: one reviewer decision on one field (CONFIRM, CORRECT, REJECT, ADD). Never edited."""

    __tablename__ = "dps_review_decision"

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(Varchar(36), index=True)
    run_no: Mapped[int] = mapped_column(Integer)
    field_id: Mapped[str] = mapped_column(Varchar(64))
    reviewer: Mapped[str] = mapped_column(Varchar(255))
    action: Mapped[str] = mapped_column(Varchar(16))
    old_value_json: Mapped[str | None] = mapped_column(UnicodeText)
    new_value_json: Mapped[str | None] = mapped_column(UnicodeText)
    reason_code: Mapped[str | None] = mapped_column(Varchar(32))
    comment: Mapped[str | None] = mapped_column(Unicode(2000))
    sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class GoldenCandidate(Base):
    """Phase 5: a correction queued as a possible golden-set case; an analyst accepts or rejects it."""

    __tablename__ = "dps_golden_candidate"

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(Varchar(36))
    field_id: Mapped[str] = mapped_column(Varchar(64))
    job_type: Mapped[str] = mapped_column(Varchar(32))
    payload_json: Mapped[str] = mapped_column(UnicodeText)
    status: Mapped[str] = mapped_column(Varchar(16), default="PENDING")  # PENDING | ACCEPTED | REJECTED
    decided_by: Mapped[str | None] = mapped_column(Varchar(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class PushCursor(Base):
    """G6: how far the event feed has been pushed to Spring (at-least-once; Spring de-duplicates by eventId)."""

    __tablename__ = "dps_push_cursor"

    name: Mapped[str] = mapped_column(Varchar(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class Leader(Base):
    """Phase 6: the scheduler lock. The holder runs aging, the outbox and the daily seal; others only run jobs."""

    __tablename__ = "dps_leader"

    name: Mapped[str] = mapped_column(Varchar(32), primary_key=True)
    holder: Mapped[str] = mapped_column(Varchar(64))
    until: Mapped[datetime] = mapped_column(UtcDateTime())


class AuditSeal(Base):
    """Phase 6: one seal per day: the hash chain over that day's audit events and review decisions."""

    __tablename__ = "dps_audit_seal"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    rows: Mapped[int] = mapped_column(Integer)
    prev_hash: Mapped[str] = mapped_column(Varchar(64))
    head_hash: Mapped[str] = mapped_column(Varchar(64))
    file_sha256: Mapped[str | None] = mapped_column(Varchar(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class LabelMemory(Base):
    """Phase 3 borrower memory: a statement wording a person confirmed for a CP's standard line.

    "Borrowings - non-current" confirmed as LT_DEBT for CP-1001 this year maps on its own next year, so the same
    borrower's next statement is read the way a person already read it. Stored in its meaning-normalised form
    (similarity.canonical), so small changes of wording ("Borrowings (non-current)") still match.
    """

    __tablename__ = "dps_label_memory"
    __table_args__ = (Index("ux_dps_label_memory", "cp_id", "label", unique=True),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    cp_id: Mapped[str] = mapped_column(Varchar(64))
    label: Mapped[str] = mapped_column(Unicode(300))
    key: Mapped[str] = mapped_column(Varchar(64))
    source_label: Mapped[str] = mapped_column(Unicode(300))
    document_id: Mapped[str | None] = mapped_column(Varchar(36))
    confirmed_by: Mapped[str] = mapped_column(Varchar(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class StatementValue(Base):
    """One line item for one entity and period. A newer value for the same key supersedes the old one."""

    __tablename__ = "dps_statement_value"
    __table_args__ = (Index("ix_dps_value_lookup", "cp_id", "key", "as_of_date", "superseded"),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    cp_id: Mapped[str] = mapped_column(Varchar(64))  # CP id or obligor id (entity key)
    key: Mapped[str] = mapped_column(Varchar(64))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(Varchar(32))
    currency: Mapped[str | None] = mapped_column(Varchar(3))
    as_of_date: Mapped[date] = mapped_column(Date)
    # Months a profit or cash-flow figure covers (3, 6, 9, 12); None for a balance or when the statement does not say.
    period_months: Mapped[int | None] = mapped_column(Integer)
    derived: Mapped[bool] = mapped_column(Boolean, default=False)
    # DOCUMENT | USER | COMPARATIVE (last year's or the last full year's column of a statement: an input to the
    # twelve-month figures, never served to Core as a figure of its own)
    source_type: Mapped[str] = mapped_column(Varchar(16))
    document_id: Mapped[str | None] = mapped_column(Varchar(36))
    page: Mapped[int | None] = mapped_column(Integer)
    cell: Mapped[str | None] = mapped_column(Varchar(64))
    note: Mapped[str | None] = mapped_column(Unicode(2000))
    entered_by: Mapped[str | None] = mapped_column(Varchar(255))
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "dps_audit_event"

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(Varchar(255))
    action: Mapped[str] = mapped_column(Varchar(64))
    target_type: Mapped[str] = mapped_column(Varchar(32))
    target_id: Mapped[str] = mapped_column(Varchar(64))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class AiCache(Base):
    """Phase 3a: a model answer kept by the hash of the model and the (masked) messages; the same question is not
    paid for twice."""

    __tablename__ = "dps_ai_cache"

    key: Mapped[str] = mapped_column(Varchar(64), primary_key=True)
    model: Mapped[str] = mapped_column(Varchar(128))
    answer: Mapped[str] = mapped_column(UnicodeText)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class AiUsage(Base):
    """Tokens and cost of the model calls made for one request (review GAP-05)."""

    __tablename__ = "dps_ai_usage"
    __table_args__ = (Index("ix_dps_ai_usage_at", "at"),)

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    purpose: Mapped[str] = mapped_column(Varchar(24))  # ASK_AI | AGREEMENT_QA | SUGGESTIONS | NARRATIVE
    user: Mapped[str] = mapped_column(Varchar(255))
    cp_id: Mapped[str | None] = mapped_column(Varchar(64))
    document_id: Mapped[str | None] = mapped_column(Varchar(36))
    job_id: Mapped[str | None] = mapped_column(Varchar(36))
    model: Mapped[str] = mapped_column(Varchar(128))
    calls: Mapped[int] = mapped_column(Integer, default=1)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float)  # in the currency of the configured rates


class AiCall(Base):
    """One model call, kept so any AI answer can be traced: what was asked, what came back, which model and prompt
    version, how long it took and how it ended. ``dps_ai_usage`` keeps the totals per request."""

    __tablename__ = "dps_ai_call"
    __table_args__ = (Index("ix_dps_ai_call_at", "at"), Index("ix_dps_ai_call_document", "document_id"),
                      Index("ix_dps_ai_call_trace", "trace_id"))

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    purpose: Mapped[str] = mapped_column(Varchar(24))
    user: Mapped[str] = mapped_column(Varchar(255))
    cp_id: Mapped[str | None] = mapped_column(Varchar(64))
    document_id: Mapped[str | None] = mapped_column(Varchar(36))
    job_id: Mapped[str | None] = mapped_column(Varchar(36))
    request_id: Mapped[str | None] = mapped_column(Varchar(64))
    model: Mapped[str] = mapped_column(Varchar(128))
    prompt_version: Mapped[str] = mapped_column(Varchar(16))  # first 8 characters of the system prompt's hash
    status: Mapped[str] = mapped_column(Varchar(8))  # OK | ERROR
    error: Mapped[str | None] = mapped_column(Unicode(500))
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float)
    request_text: Mapped[str | None] = mapped_column(UnicodeText)  # the messages sent, secrets masked
    response_text: Mapped[str | None] = mapped_column(UnicodeText)
    # OpenTelemetry ids of the model call (observability, 23 Nov 2026): the row opens its trace in Langfuse.
    trace_id: Mapped[str | None] = mapped_column(Varchar(32))
    span_id: Mapped[str | None] = mapped_column(Varchar(16))


class TraceEvent(Base):
    """A document's history in order: uploaded, pages read, each covenant or figure found, flags, the AI reader's
    verdict, the outcome, and every review and correction a person made (the History section of the document)."""

    __tablename__ = "dps_trace_event"
    __table_args__ = (Index("ix_dps_trace_document", "document_id", "at"),
                      Index("ix_dps_trace_event_trace", "trace_id"))

    id: Mapped[str] = mapped_column(Varchar(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(Varchar(36))
    job_id: Mapped[str | None] = mapped_column(Varchar(36))
    at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    step: Mapped[str] = mapped_column(Varchar(16))  # UPLOAD | READ | FOUND | SHOWN | VALUE | MISSING | FLAG | AI ...
    actor: Mapped[str] = mapped_column(Varchar(255))
    summary: Mapped[str] = mapped_column(Unicode(1000))
    detail_json: Mapped[str] = mapped_column(UnicodeText, default="{}")
    trace_id: Mapped[str | None] = mapped_column(Varchar(32))  # the job's trace (observability, 23 Nov 2026)

