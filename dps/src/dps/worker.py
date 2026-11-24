"""Background workers (03-flows-design.md §4). Run: ``python -m dps.worker`` (``DPS_WORKER_POOL=HEAVY`` for Docling).

Each loop: the leader's scheduler tasks (event push, queue ageing, reaper, audit seal), then claim one job and run
it in the one pipeline (full review WP7): ANALYZE, or the job type's skill pack. Transient failures (Core down,
file store hiccup) put the job back in the queue until ``worker_max_attempts``; deterministic outcomes
(FAILED, NEEDS_REVIEW) end the attempt.
"""

from __future__ import annotations

import hashlib
import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, sessionmaker

from dps import jobs
from dps.agent import usage
from dps.db import Database, require_snapshot_isolation
from dps.ingest.file_store import FileStore
from dps.ingest.pipeline import QUARANTINE, IngestFailed, Ingestor
from dps.integrations.spring import SpringClient, SpringUnavailable
from dps.models import Document, Job, utcnow
from dps.ops import observability as obs
from dps.profiles import currency_words
from dps.settings import Settings, get_settings, llm_allowed
from dps.skills.statement_lines.reader import ContentFailed, NeedsHeavyPool

log = logging.getLogger(__name__)

SKILL_FLOWS = ("SKILL_PACK",)  # every job type that reads a document (one pipeline, full review WP7)
# Spring refuses the content of an event push: the bad events are recorded and skipped (E1), never retried for ever.
REJECTED_EVENTS = frozenset({400, 409, 413, 415, 422})
ACTOR = "dps-worker"


class Worker:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], core: SpringClient,
                 runners: dict | None = None, ingestor: Ingestor | None = None,
                 page_models: dict[str, dict] | None = None,
                 analyzer: Callable[[Session, Job, dict], None] | None = None,
                 pack_runner: Callable[[Session, Job, dict | None, bytes | None], None] | None = None) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.core = core
        self.runners = runners or {}  # unused since the one pipeline (full review WP7); kept for callers
        self.ingestor = ingestor
        # sha256 -> page model built by the ingest step; the flows' parser reads it instead of parsing again.
        self.page_models = page_models if page_models is not None else {}
        self.analyzer = analyzer
        self.pack_runner = pack_runner

    _last_aging: float = 0.0
    _last_seal: float = 0.0
    _leader_until: float = 0.0
    _leader: bool = False

    def _is_leader(self) -> bool:
        """Phase 6: one worker runs the scheduler tasks; the lock is re-checked every 10 s (lease 60 s)."""
        now = time.monotonic()
        if now < self._leader_until:
            return self._leader
        from dps.ops.production import acquire_leader

        try:
            with self.session_factory() as db:
                self._leader = acquire_leader(db, "scheduler", self.settings.worker_id, 60)
        except Exception:  # noqa: BLE001 - a lock hiccup only skips this round of scheduler tasks
            log.warning("scheduler lock check failed", exc_info=True)
            self._leader = False
        self._leader_until = now + 10
        return self._leader

    _push_after: float = 0.0

    def push_events(self) -> int:
        """G6: push the event feed to Spring (optional). In order, from the stored cursor; the cursor only moves after
        Spring answered 2xx, so a failure is retried (at least once). Spring de-duplicates by eventId."""
        path = self.settings.events_push_path
        if not path or time.monotonic() < self._push_after:
            return 0
        from dps import feed
        from dps.models import PushCursor

        pushed = 0
        with self.session_factory() as db:
            cursor = db.get(PushCursor, "spring")
            if cursor is None:
                cursor = PushCursor(name="spring", seq=0)
                db.add(cursor)
                db.commit()
            events, last = feed.read(db, cursor.seq, self.settings.events_push_batch)
            if not events:
                return 0
            try:
                resp = self.core._request("POST", path, json={"events": events})
            except (SpringUnavailable, httpx.HTTPError) as ex:
                log.warning("event push to Spring failed (%d events, retried later): %s", len(events),
                            type(ex).__name__)
                self._push_after = time.monotonic() + 30
                return 0
            if resp.status_code in REJECTED_EVENTS:
                # Spring refuses the content (improvement plan E1, 24 Nov 2026): before, the same batch was retried
                # every 30 s for ever and no later event was ever pushed. Each event is sent alone; the ones Spring
                # still refuses are recorded (audit EVENT_PUSH_REJECTED, metric) and skipped. They stay in the
                # feed, so GET /dps/v1/events still serves them.
                pushed = self._push_one_by_one(db, path, events)
            elif resp.status_code >= 400:  # 401/403/404/408/429: configuration or load, never skip: retry, alert
                log.error("event push refused by Spring: HTTP %s (%d events wait; check the token and the path)",
                          resp.status_code, len(events))
                obs.count("dps.events.push_refused", 1, status=str(resp.status_code))
                self._push_after = time.monotonic() + 30
                return 0
            else:
                pushed = len(events)
            cursor.seq, cursor.updated_at = last, utcnow()
            db.commit()
        return pushed

    def _push_one_by_one(self, db: Session, path: str, events: list[dict]) -> int:
        from dps import services

        delivered = 0
        for event in events:
            try:
                resp = self.core._request("POST", path, json={"events": [event]})
            except (SpringUnavailable, httpx.HTTPError) as ex:
                raise SpringUnavailable(f"event push interrupted: {type(ex).__name__}") from ex
            if resp.status_code < 400:
                delivered += 1
            elif resp.status_code in REJECTED_EVENTS:
                services.audit(db, "dps-worker", "EVENT_PUSH_REJECTED", "event", str(event.get("eventId")),
                               seq=event.get("seq"), type=event.get("type"), status=resp.status_code)
                obs.count("dps.events.push_rejected", 1, type=str(event.get("type")))
                log.error("event %s (%s) refused by Spring with HTTP %s: recorded and skipped",
                          event.get("eventId"), event.get("type"), resp.status_code)
            else:
                raise SpringUnavailable(f"event push: HTTP {resp.status_code}")
        return delivered

    def _seal(self) -> None:
        """Once an hour: seal every finished day not sealed yet (the audit hash chain)."""
        now = time.monotonic()
        if now - self._last_seal < 3600:
            return
        self._last_seal = now
        from dps.ops.production import seal_pending

        try:
            with self.session_factory() as db:
                seal_pending(db, self.settings.seals_dir)
        except Exception:  # noqa: BLE001 - retried next hour; alerting watches the seal age
            log.warning("audit seal failed", exc_info=True)

    def run_once(self) -> bool:
        """Scheduler tasks (leader only), then at most one job. Returns True when a job was processed."""
        if self._is_leader():
            self.push_events()
            self._age_queue()
            self._reap()
            self._seal()
        limits = jobs.Limits(per_tenant=self.settings.queue_max_running_per_tenant,
                             per_batch=self.settings.queue_max_running_per_batch)
        with self.session_factory() as db:
            job = jobs.claim(db, self.settings.worker_id, self.settings.worker_pool,
                             self.settings.worker_lease_seconds, self.settings.worker_max_attempts,
                             limits=limits, poison_crashes=self.settings.worker_poison_crashes)
            if job is None:
                return False
            self._process(db, job)
            return True

    def _age_queue(self) -> None:
        """Once a minute: waiting jobs climb one priority step per ``queue_aging_minutes`` waited."""
        now = time.monotonic()
        if now - self._last_aging < 60:
            return
        self._last_aging = now
        with self.session_factory() as db:
            jobs.age(db, self.settings.queue_aging_minutes)
            self._queue_metrics(db)

    def _reap(self) -> None:
        """G6: jobs whose worker died on their last attempt end DEAD instead of showing "in progress" for ever."""
        try:
            with self.session_factory() as db:
                if jobs.reap(db, self.settings.worker_max_attempts):
                    log.warning("set aside jobs whose last attempt lost its worker")
                from dps.agent.guard import purge_cache

                purge_cache(db, self.settings.ai_cache_days)  # G17: no model answer kept past its days
        except Exception:  # noqa: BLE001 - a scheduler task never stops the worker
            log.warning("reaper failed", exc_info=True)

    @staticmethod
    def _queue_metrics(db: Session) -> None:
        """Phase 6: the autoscaling and alert signals, once a minute from the leader."""
        from dps.ops.production import scaling

        try:
            for pool in scaling(db):
                obs.observe("dps.queue.oldest_seconds", pool["oldestQueuedSeconds"], pool=pool["pool"])
                obs.observe("dps.queue.depth_per_worker", pool["depthPerWorker"], pool=pool["pool"])
            dead = db.scalar(select(func.count()).select_from(Job).where(Job.status == jobs.DEAD)) or 0
            obs.observe("dps.jobs.dead", dead)
            from dps.ops.production import review_backlog

            backlog = review_backlog(db)  # B4: jobs waiting for a person, and the oldest wait
            obs.observe("dps.review.backlog", backlog["waiting"])
            obs.observe("dps.review.mapping_backlog", backlog["mappingReview"])
            obs.observe("dps.review.oldest_wait_seconds", backlog["oldestWaitSeconds"])
            from dps.models import AuditSeal, ReviewDecision

            rows = db.execute(select(Job.job_type, ReviewDecision.action, func.count())
                              .join(Job, Job.id == ReviewDecision.job_id).where(ReviewDecision.sampled == true())
                              .group_by(Job.job_type, ReviewDecision.action)).all()
            per: dict[str, list[int]] = {}
            for job_type, action, n in rows:
                acc = per.setdefault(job_type, [0, 0])
                acc[0] += n
                acc[1] += n if action in ("CORRECT", "REJECT") else 0
            for job_type, (n, wrong) in per.items():
                obs.observe("dps.review.error_rate", wrong / n, job_type=job_type)
            last_seal = db.scalar(select(func.max(AuditSeal.day)))
            if last_seal is not None:
                stamp = datetime.combine(last_seal, datetime.min.time(), tzinfo=UTC).timestamp()
                obs.observe("dps.audit_seal.last_day_timestamp", stamp)
        except Exception:  # noqa: BLE001 - metrics never stop the scheduler
            log.warning("queue metrics failed", exc_info=True)

    def _process(self, db: Session, job: Job) -> None:
        doc = db.get(Document, job.document_id)
        token = jobs.worker_attempt.set((job.id, self.settings.worker_id, job.attempts))
        from dps.integrations.spring import CORRELATION

        correlation = CORRELATION.set(job.correlation_id)  # every Spring call of this job carries it
        cancel_check = jobs.CANCEL_CHECK.set(self._cancel_check(job.id))  # AI readers stop when it is cancelled
        stop = self._start_heartbeat(job.id, job.attempts)
        # The job's trace (observability, 23 Nov 2026). In Langfuse: the trace name is the job type, the session is
        # the document (every job on one document side by side), the user is who uploaded it. Ids only: no file name.
        with obs.bound(job_id=job.id, document_id=job.document_id, user=doc.uploaded_by if doc else None), \
                obs.span("dps.job", **{"dps.flow": job.flow, "dps.pool": self.settings.worker_pool,
                                       "dps.job_type": job.job_type, "dps.job_id": job.id,
                                       "dps.document_id": job.document_id, "dps.attempt": job.attempts,
                                       "langfuse.trace.name": job.job_type,
                                       "langfuse.session.id": job.document_id,
                                       "langfuse.user.id": doc.uploaded_by if doc else None,
                                       "langfuse.trace.tags": job.flow}) as job_span:
            try:
                self._run(db, job, doc)
                obs.set_attrs(job_span, **{"dps.status": job.status, "dps.error_code": job.error_code})
                if job.status in ("FAILED", "DEAD"):
                    obs.mark_failed(job_span, job.error_code or job.status)
            except jobs.LeaseLost:
                db.rollback()
                log.warning("Discarded a stale attempt for job %s", job.id)
            finally:
                stop.set()
                jobs.worker_attempt.reset(token)
                CORRELATION.reset(correlation)
                jobs.CANCEL_CHECK.reset(cancel_check)
                self.page_models.clear()  # sequential worker: never keep a page model past its job

    def _cancel_check(self, job_id: str):
        """Reads the job's cancel flag in its own short session (the job's session holds uncommitted work)."""
        def check() -> bool:
            with self.session_factory() as s:
                return jobs.cancelled(s, job_id)

        return check

    def _run(self, db: Session, job: Job, doc: Document | None) -> None:
        if job.flow == "ANALYZE":
            self._analyze(db, job, doc)
            return
        if job.flow in SKILL_FLOWS:
            self._analyze(db, job, doc, runner=self.pack_runner)
            return
        jobs.finish(db, job, jobs.FAILED, f"No flow registered for {job.flow}", "UNSUPPORTED_JOB_TYPE")

    def _analyze(self, db: Session, job: Job, doc: Document | None, runner=None) -> None:
        """Phase 3 ANALYZE (page model -> classification and identifiers -> run + dps.analysis.completed) and
        Phase 4 skill packs (page model -> generic flow -> run + extraction event)."""
        pack_job = job.flow in SKILL_FLOWS
        if job.cancel_requested:
            jobs.finish(db, job, jobs.CANCELLED, "Cancelled before it started", "JOB_CANCELLED")
            return
        target = runner if pack_job else self.analyzer
        if target is None or self.ingestor is None:
            jobs.finish(db, job, jobs.FAILED, f"{job.job_type} is not configured on this worker",
                        "UNSUPPORTED_JOB_TYPE")
            return
        raw = None
        if job.job_type in ("DRAFT_COMMENTARY", "RECONCILE_SET"):  # a JSON manifest: hash-checked, no page model
            try:
                raw = self.ingestor.read(doc)
            except IngestFailed as ex:
                jobs.finish(db, job, jobs.FAILED, str(ex)[:2000], ex.code)
                return
        elif not self._ingest(db, job, doc):
            return
        jobs.record_step(db, job, "START", attempt=job.attempts, worker=self.settings.worker_id)
        started = time.monotonic()
        from dps.agent import vision
        from dps.ingest.inspect import InspectionFailed
        from dps.parse.sandbox import SandboxError

        ingestor = self.ingestor
        images = vision.page_images.set(  # scanned pages: rendered only if the vision model is asked to read them
            vision.renderer(lambda: ingestor.read(doc, staged=not doc.object_key)) if doc is not None else None)
        usage.begin(job.tenant_id)  # every Document Agent call of this job: tokens and cost per job (dps_ai_usage)
        try:
            with obs.span("dps.skill" if pack_job else "dps.analyze"):
                if pack_job:
                    target(db, job, self.page_models.get(doc.sha256), raw)
                else:
                    target(db, job, self.page_models[doc.sha256])
        except jobs.LeaseLost:
            raise
        except jobs.JobCancelled:  # cancelled or replaced while reading: no result is saved, no more AI calls (S10)
            db.rollback()
            db.refresh(job)
            jobs.finish(db, job, jobs.CANCELLED, "Cancelled while it was being read", "JOB_CANCELLED")
            self._sync_document(db, job)
            return
        except NeedsHeavyPool as ex:  # a statement the standard reader cannot read: the HEAVY pool reads it
            db.rollback()
            db.refresh(job)
            obs.event("MOVED_HEAVY", f"{ex}: moved to the HEAVY pool", code="NEEDS_HEAVY")
            jobs.move_to_pool(db, job, "HEAVY")
            return
        except ContentFailed as ex:  # no readable lines: a content failure, not a crash
            db.rollback()
            db.refresh(job)
            jobs.finish(db, job, jobs.FAILED, str(ex)[:2000] or "No line items found", "CONTENT_FAILED")
            self._sync_document(db, job)
            return
        except (SandboxError, InspectionFailed) as ex:
            db.rollback()
            db.refresh(job)
            if self.settings.worker_pool != "HEAVY" and ex.code in ("PARSE_TIMEOUT", "OUT_OF_MEMORY"):
                obs.event("MOVED_HEAVY", f"{ex.code}: moved to the HEAVY pool", code=ex.code)
                jobs.move_to_pool(db, job, "HEAVY")
                return
            if ex.code == "PARSER_CRASH":
                jobs.release_for_retry(db, job, str(ex)[:2000], min(2, self.settings.worker_max_attempts), ex.code,
                                       self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            else:
                jobs.finish(db, job, jobs.FAILED, str(ex)[:2000], ex.code,
                            payload={"quarantine": ex.code in QUARANTINE})
            self._sync_document(db, job)
            return
        except (SpringUnavailable, httpx.HTTPError, OSError) as ex:
            # the detail (URLs, paths) stays in the log; the job and its events carry a plain message only
            # (CTS review, 23 Nov 2026: exception text reached the bank app's logs)
            log.warning("job %s: a service did not answer: %s", job.id, ex)
            db.rollback()
            db.refresh(job)
            jobs.release_for_retry(db, job, f"A service DPS needs did not answer ({type(ex).__name__}); retried.",
                                   self.settings.worker_max_attempts, "SPRING_UNAVAILABLE",
                                   self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            return
        except Exception as ex:  # a bug: retry once, then DEAD
            log.exception("job %s crashed", job.id)
            db.rollback()
            db.refresh(job)
            jobs.release_for_retry(db, job, f"Internal error ({type(ex).__name__}); the support team has the log.",
                                   min(2, self.settings.worker_max_attempts), "INTERNAL_ERROR",
                                   self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            return
        finally:
            vision.page_images.reset(images)  # release the source renderer at the end of the job
            usage.record(db, self.settings, job.job_type[:24], doc.uploaded_by if doc else ACTOR,
                         document_id=job.document_id, job_id=job.id)
        seconds = round(time.monotonic() - started, 2)
        obs.observe("dps.document.duration", seconds, flow="ANALYZE", pool=self.settings.worker_pool)

    def _ingest(self, db: Session, job: Job, doc: Document | None) -> bool:
        """Phase 2: hash check, inspection and the sandboxed page model, for jobs whose file is in the DMS store.
        Returns False when the job ended or went back to the queue."""
        if self.ingestor is None or doc is None or not (doc.object_key or job.job_type == "ANALYZE"):
            return True
        try:
            with obs.span("dps.ingest", **{"dps.pool": self.settings.worker_pool}) as ingest_span:
                data = self.ingestor.read(doc, staged=not doc.object_key)
                model = self.ingestor.page_model(db, job, doc, data)
                pages = (model or {}).get("pages") or []
                methods = [str(p.get("sourceMethod") or p.get("method") or "") for p in pages]
                obs.set_attrs(ingest_span, **{
                    "dps.bytes": len(data or b""), "dps.pages": len(pages),
                    "dps.pages_ocr": sum(m == "OCR" for m in methods),
                    "dps.pages_docling": sum(m == "DOCLING" for m in methods),
                    "dps.pages_blank": sum(m == "BLANK" for m in methods),
                    "dps.flags": ",".join((model or {}).get("flags") or []) or None})
        except IngestFailed as ex:
            db.refresh(job)
            jobs.record_step(db, job, "INGEST", error=ex.code)
            if ex.heavy:  # a scan too slow for the LIGHT pool: the HEAVY pool reads it with longer limits
                obs.event("MOVED_HEAVY", f"{ex.code}: moved to the HEAVY pool", code=ex.code)
                jobs.move_to_pool(db, job, "HEAVY")
                return False
            if ex.retry:  # a parser crash is retried once; the second crash on the same file is DEAD (poison)
                jobs.release_for_retry(db, job, str(ex)[:2000], min(2, self.settings.worker_max_attempts), ex.code,
                                       self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            else:
                quarantine = ex.code in QUARANTINE
                obs.event("INGEST_REJECTED", f"{ex.code}: {ex}", code=ex.code, quarantine=quarantine)
                if ex.code == "INTEGRITY_MISMATCH":
                    log.error("Job %s: stored file does not match its SHA-256 (ops alert)", job.id)
                jobs.finish(db, job, jobs.FAILED, str(ex)[:2000], ex.code,
                            payload={"quarantine": quarantine})  # always present (CTS review, 23 Nov 2026)
            self._sync_document(db, job)
            return False
        self.page_models[doc.sha256] = model
        return True

    def _start_heartbeat(self, job_id: str, attempt: int) -> threading.Event:
        """Renews the job's lease every third of its length until the returned event is set."""
        stop = threading.Event()
        lease = self.settings.worker_lease_seconds

        def beat() -> None:
            while not stop.wait(max(1.0, lease / 3)):
                try:
                    with self.session_factory() as db:
                        if not jobs.renew_lease(db, job_id, self.settings.worker_id, lease, attempt):
                            log.warning("Job %s: lease lost to another worker", job_id)
                            return
                except Exception:  # noqa: BLE001 - a missed beat only shortens the lease
                    log.warning("Job %s: lease renewal failed", job_id, exc_info=True)

        threading.Thread(target=beat, name=f"lease-{job_id[:8]}", daemon=True).start()
        return stop

    def _sync_document(self, db: Session, job: Job) -> None:
        if job.status in (jobs.FAILED, jobs.DEAD):  # the screens show both as a failed document
            doc = db.get(Document, job.document_id)
            if doc is not None:
                doc.status = "FAILED"
                db.commit()


class PageModelFirst:
    """The statement reader's parser: the page model the ingest step built for these bytes, else the given
    parser."""

    def __init__(self, fallback, page_models: dict[str, dict]) -> None:
        self.fallback, self.page_models = fallback, page_models

    def parse(self, data: bytes):
        from dps.parse.page_model import to_pages

        model = self.page_models.get(hashlib.sha256(data).hexdigest())
        if model is not None and model.get("format") == "PDF":
            return to_pages(model)
        return self.fallback.parse(data)


def build_ingestor(settings: Settings) -> Ingestor:
    from dps.ingest.pipeline import sandbox_parser
    from dps.ingest.store import LocalStore

    store = LocalStore(settings.storage_dir, settings.staging_dir, settings.derived_dir,
                       settings.max_file_mb * 1024 * 1024)
    return Ingestor(store, settings.pipeline_version, sandbox_parser(settings.worker_pool, settings.parser_socket),
                    ocr=settings.ocr_enabled, heavy=settings.worker_pool == "HEAVY")


def build_analyzer(settings: Settings, database: Database) -> Callable[[Session, Job, dict], None]:
    """ANALYZE on the model gateway (small route) and Spring's lookup tool."""
    from dps.agent.gateway import from_settings
    from dps.analyze import analysis
    from dps.integrations.spring_tools import SpringTools

    gateway = from_settings(settings, database.session_factory) if llm_allowed(settings) else None
    tools = SpringTools.from_settings(settings)

    def run(db: Session, job: Job, model: dict) -> None:
        from dps.flows.skill_job import run_skill_job

        run_skill_job(job.id, job.job_type,
                      lambda: analysis.analyze(model, job, gateway, tools, settings.pipeline_version,
                                               settings.loan_account_pattern or None,
                                               settings.facility_id_pattern or None),
                      lambda result: analysis.finish(db, job, result))

    return run


def statement_deps(settings: Settings, database: Database, core: SpringClient,
                   page_models: dict[str, dict] | None = None, gateway=None):
    """Wire sandboxed statement parsing, optional AI, and borrower settings from Core."""
    from dps.ingest.inspect import declared_format
    from dps.ingest.pipeline import page_hint, sandbox_parser
    from dps.parse.sandbox import ParseRequest
    from dps.skills.statement_lines.reader import StatementDeps

    heavy = settings.worker_pool == "HEAVY"
    parse = sandbox_parser(settings.worker_pool, settings.parser_socket)

    def sandbox_read(data: bytes, ext: str, entity: str, style: str) -> dict:
        fmt = declared_format("statement" + ext)
        req = ParseRequest(data, fmt, hashlib.sha256(data).hexdigest(), ocr=settings.ocr_enabled, heavy=heavy,
                           statement=(ext, entity, style))
        return parse(req, page_hint(data, fmt))

    store = FileStore(settings.storage_dir)

    def read_file(doc: Document) -> bytes:
        return store.path(doc.storage_path).read_bytes()

    second_reader = None
    if settings.statement_ai_check and gateway is not None:  # pragma: no cover - live model
        from dps.agent.document_agent import completion as agent_completion
        from dps.extraction.statement_ai import check

        completion = agent_completion(gateway, "strong", "statement_lines")  # the Document Agent, statement skill

        def second_reader(text, values, scale=1.0, entity=None):
            return check(text, values, completion, entity or settings.statement_entity, scale)

    return StatementDeps(database.session_factory, read_file, None, heavy, second_reader=second_reader,
                         entity=settings.statement_entity, fiscal_year_end_month=settings.fiscal_year_end_month,
                         loan_currency=core.cp_currency, fx_rate=core.fx_rate,
                         default_currency=settings.default_currency,
                         loan_profile=core.cp_profile, currency_words=lambda: currency_words(core.country_profiles()),
                         report_derived_values=settings.report_derived_values, sandbox_read=sandbox_read,
                         skip_confident_ai=settings.ai_skip_confident)


def build_pack_runner(settings: Settings, database: Database, core: SpringClient | None = None,
                      page_models: dict[str, dict] | None = None):
    """The one pipeline (full review WP7): every job type that reads a document runs its skill pack in the skills
    flow (EXTRACT_AGREEMENT, EXTRACT_STATEMENT, EXTRACT_SANCTION, EXTRACT_CERTIFICATE, EXTRACT_AMENDMENT,
    EXTRACT_WAIVER, ONBOARD, RECONCILE_SET, DRAFT_COMMENTARY)."""
    from dps.agent.gateway import from_settings
    from dps.integrations.spring_tools import SpringTools
    from dps.skills import runner as skills

    gateway = from_settings(settings, database.session_factory) if llm_allowed(settings) else None
    tools = SpringTools.from_settings(settings)
    statements = statement_deps(settings, database, core or SpringClient.from_settings(settings), page_models,
                                gateway)

    def run(db: Session, job: Job, model: dict | None, raw: bytes | None) -> None:
        from dps.flows.skill_job import run_skill_job

        run_skill_job(job.id, job.job_type,
                      lambda: skills.run_job(job, model, gateway, tools, db, settings.pipeline_version, raw,
                                             scorer_dir=settings.scorer_dir, statement_deps=statements),
                      lambda result: skills.finish(db, job, result))

    return run


def main() -> None:  # pragma: no cover - process entry point
    from dps.skills.covenants.taxonomy import get as get_taxonomy

    get_taxonomy()  # fail startup before claiming any jobs if the taxonomy is missing or invalid
    settings = get_settings()
    obs.setup("dps-worker-heavy" if settings.worker_pool == "HEAVY" else "dps-worker")
    database = Database(settings.database_url)
    require_snapshot_isolation(database.engine)
    core = SpringClient.from_settings(settings)
    page_models: dict[str, dict] = {}
    ingestor = build_ingestor(settings) if settings.ingest_enabled else None
    worker = Worker(settings, database.session_factory, core, {}, ingestor, page_models,
                    build_analyzer(settings, database), build_pack_runner(settings, database, core, page_models))
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    log.info("DPS worker %s started (pool=%s)", settings.worker_id, settings.worker_pool)
    while not stop["flag"]:
        if not worker.run_once():
            time.sleep(2)


if __name__ == "__main__":  # pragma: no cover
    main()
