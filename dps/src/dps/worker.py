"""Background workers (03-flows-design.md §4). Run: ``python -m dps.worker`` (``DPS_WORKER_POOL=HEAVY`` for Docling).

Each loop: send pending C5 events, then claim one job and run its flow. Transient failures (Core down,
file store hiccup) put the job back in the queue until ``worker_max_attempts``; deterministic outcomes
(FAILED, NEEDS_REVIEW) end the attempt.
"""

from __future__ import annotations

import hashlib
import json
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
from dps.integrations.core import CoreClient, CoreUnavailable
from dps.models import Document, EventOutbox, Job, utcnow
from dps.ops import observability as obs
from dps.profiles import currency_words
from dps.settings import Settings, get_settings, llm_allowed

log = logging.getLogger(__name__)

# flow name → callable(job, settings) -> outcome ("PUBLISHED" | "REPORTED" | "NEEDS_REVIEW" | "FAILED" | "HEAVY")
FlowRunner = Callable[[Job], tuple[str, str, str]]  # (outcome, flow_state_id, error)

OUTCOME_TO_STATUS = {"PUBLISHED": jobs.DONE, "REPORTED": jobs.DONE, "NEEDS_REVIEW": jobs.NEEDS_REVIEW,
                     "FAILED": jobs.FAILED}
# What the AI reader of each flow is recorded as in dps_ai_usage / dps_ai_call.
AI_PURPOSE = {"AGREEMENT_INTAKE": "AGREEMENT_READ", "STATEMENT_PROCESSING": "STATEMENT_READ"}
ACTOR = "dps-worker"


class Worker:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], core: CoreClient,
                 runners: dict[str, FlowRunner], ingestor: Ingestor | None = None,
                 page_models: dict[str, dict] | None = None,
                 analyzer: Callable[[Session, Job, dict], None] | None = None,
                 pack_runner: Callable[[Session, Job, dict | None, bytes | None], None] | None = None) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.core = core
        self.runners = runners
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
                resp.raise_for_status()
            except (CoreUnavailable, httpx.HTTPError) as ex:
                log.warning("event push to Spring failed (%d events, retried later): %s", len(events),
                            type(ex).__name__)
                self._push_after = time.monotonic() + 30
                return 0
            cursor.seq, cursor.updated_at = last, utcnow()
            db.commit()
            pushed = len(events)
        return pushed

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
            self.send_events()
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
        stop = self._start_heartbeat(job.id, job.attempts)
        with obs.bound(job_id=job.id, document_id=job.document_id, user=doc.uploaded_by if doc else None), \
                obs.span("dps.job", **{"dps.flow": job.flow, "dps.pool": self.settings.worker_pool}):
            try:
                self._run(db, job, doc)
            except jobs.LeaseLost:
                db.rollback()
                log.warning("Discarded a stale attempt for job %s", job.id)
            finally:
                stop.set()
                jobs.worker_attempt.reset(token)
                self.page_models.clear()  # sequential worker: never keep a page model past its job

    def _run(self, db: Session, job: Job, doc: Document | None) -> None:
        if job.flow == "ANALYZE":
            self._analyze(db, job, doc)
            return
        if job.flow == "SKILL_PACK":
            self._analyze(db, job, doc, runner=self.pack_runner)
            return
        runner = self.runners.get(job.flow)
        if runner is None:
            jobs.finish(db, job, jobs.FAILED, f"No flow registered for {job.flow}", "UNSUPPORTED_JOB_TYPE")
            return
        if job.cancel_requested:
            jobs.finish(db, job, jobs.CANCELLED, "Cancelled before it started", "JOB_CANCELLED")
            return
        if not self._ingest(db, job, doc):
            return
        jobs.record_step(db, job, "START", attempt=job.attempts, worker=self.settings.worker_id)
        obs.event("JOB_START", f"{job.flow} attempt {job.attempts} on {self.settings.worker_id} "
                  f"({self.settings.worker_pool} pool)", flow=job.flow, pool=self.settings.worker_pool)
        started = time.monotonic()
        usage.begin()
        try:
            outcome, state_id, error = runner(job)
        except jobs.LeaseLost:
            raise
        except (CoreUnavailable, httpx.HTTPError, OSError) as ex:
            log.warning("Job %s attempt %d failed: %s", job.id, job.attempts, type(ex).__name__)
            db.refresh(job)
            jobs.record_step(db, job, "RETRY", error=str(ex)[:500])
            code = "STORAGE_UNAVAILABLE" if isinstance(ex, OSError) else "SPRING_UNAVAILABLE"
            jobs.release_for_retry(db, job, str(ex)[:2000], self.settings.worker_max_attempts, code,
                                   self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            self._sync_document(db, job)
            return
        except Exception as ex:  # a bug: retry once, then DEAD; the worker stays alive either way
            log.exception("Job %s crashed", job.id)
            db.refresh(job)
            message = f"{type(ex).__name__}: {str(ex)[:1900]}"
            jobs.record_step(db, job, "RETRY", error=message[:500])
            jobs.release_for_retry(db, job, message, min(2, self.settings.worker_max_attempts), "INTERNAL_ERROR",
                                   self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            self._sync_document(db, job)
            return
        finally:
            # The AI readers' calls for this document (AGREEMENT_READ / STATEMENT_READ), even when the job failed.
            usage.record(db, self.settings, AI_PURPOSE.get(job.flow, job.flow[:24]),
                         doc.uploaded_by if doc else ACTOR, document_id=job.document_id, job_id=job.id)
        db.refresh(job)
        job.flow_state_id = state_id
        seconds = round(time.monotonic() - started, 2)
        jobs.record_step(db, job, "END", outcome=outcome, seconds=seconds)
        obs.event("JOB_END", f"{job.flow} {outcome} in {seconds} s" + (f": {error}" if error else ""),
                  flow=job.flow, outcome=outcome, seconds=seconds)
        obs.count("dps.documents.processed", 1, flow=job.flow, outcome=outcome, pool=self.settings.worker_pool)
        obs.observe("dps.document.duration", seconds, flow=job.flow, pool=self.settings.worker_pool)
        if outcome == "HEAVY":
            jobs.move_to_pool(db, job, "HEAVY")
            return
        status = OUTCOME_TO_STATUS.get(outcome, jobs.FAILED)
        jobs.finish(db, job, status, error or None, "CONTENT_FAILED" if status == jobs.FAILED else None)

    def _analyze(self, db: Session, job: Job, doc: Document | None, runner=None) -> None:
        """Phase 3 ANALYZE (page model -> classification and identifiers -> run + dps.analysis.completed) and
        Phase 4 skill packs (page model -> generic flow -> run + extraction event)."""
        pack_job = job.flow == "SKILL_PACK"
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

        ingestor = self.ingestor
        images = vision.page_images.set(  # scanned pages: rendered only if the vision model is asked to read them
            vision.renderer(lambda: ingestor.read(doc, staged=not doc.object_key)) if doc is not None else None)
        usage.begin()  # every Document Agent call of this job: tokens and cost per job (dps_ai_usage)
        try:
            with obs.span("dps.skill" if pack_job else "dps.analyze"):
                if pack_job:
                    target(db, job, self.page_models.get(doc.sha256), raw)
                else:
                    target(db, job, self.page_models[doc.sha256])
        except jobs.LeaseLost:
            raise
        except (CoreUnavailable, httpx.HTTPError, OSError) as ex:
            db.rollback()
            db.refresh(job)
            jobs.release_for_retry(db, job, str(ex)[:2000], self.settings.worker_max_attempts, "SPRING_UNAVAILABLE",
                                   self.settings.worker_retry_base_seconds, self.settings.worker_retry_max_seconds)
            return
        except Exception as ex:  # a bug: retry once, then DEAD
            log.exception("ANALYZE job %s crashed", job.id)
            db.rollback()
            db.refresh(job)
            jobs.release_for_retry(db, job, f"{type(ex).__name__}: {str(ex)[:1900]}",
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
            with obs.span("dps.ingest"):
                data = self.ingestor.read(doc, staged=not doc.object_key)
                model = self.ingestor.page_model(db, job, doc, data)
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
                            payload={"quarantine": True} if quarantine else None)
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

    def send_events(self, limit: int = 20) -> int:
        """C5 transactional outbox: delivers unsent events in order; stops at the first transient error."""
        sent = 0
        with self.session_factory() as db:
            pending = db.scalars(select(EventOutbox).where(EventOutbox.sent_at.is_(None),
                                                           EventOutbox.attempts < 50)
                                 .order_by(EventOutbox.created_at).limit(limit)).all()
            for event in pending:
                event.attempts += 1
                payload = json.loads(event.payload_json)
                if not payload.get("dataSourceId"):
                    payload["dataSourceId"] = self.settings.core_data_source_id
                try:
                    self.core.send_data_event(payload)
                except CoreUnavailable as ex:
                    event.last_error = str(ex)[:2000]
                    db.commit()
                    break
                except httpx.HTTPStatusError as ex:  # 4xx: will not succeed on retry
                    event.last_error = f"HTTP {ex.response.status_code}: {ex.response.text[:500]}"
                    event.attempts = 50
                    db.commit()
                    continue
                event.sent_at = utcnow()
                db.commit()
                sent += 1
        return sent


class PageModelFirst:
    """The flows' parser: the page model the ingest step built for these bytes, else the given parser."""

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


def build_pack_runner(settings: Settings, database: Database):
    """Phase 4: the generic flow for EXTRACT_SANCTION, EXTRACT_CERTIFICATE, EXTRACT_AMENDMENT, DRAFT_COMMENTARY."""
    from dps.agent.gateway import from_settings
    from dps.integrations.spring_tools import SpringTools
    from dps.skills import runner as skills

    gateway = from_settings(settings, database.session_factory) if llm_allowed(settings) else None
    tools = SpringTools.from_settings(settings)

    def run(db: Session, job: Job, model: dict | None, raw: bytes | None) -> None:
        from dps.flows.skill_job import run_skill_job

        run_skill_job(job.id, job.job_type,
                      lambda: skills.run_job(job, model, gateway, tools, db, settings.pipeline_version, raw,
                                             scorer_dir=settings.scorer_dir),
                      lambda result: skills.finish(db, job, result))

    return run


def build_runners(settings: Settings, database: Database, store: FileStore, core: CoreClient,
                  page_models: dict[str, dict] | None = None) -> dict[str, FlowRunner]:
    """Wires the flows with their dependencies (imported here so the API process never loads CrewAI)."""
    from dps.extraction.statement_pdf import read_pdf_statement
    from dps.flows.intake import AgreementIntakeFlow, IntakeDeps
    from dps.flows.statement import StatementDeps, StatementProcessingFlow
    from dps.parse.engine import DoclingConverter, HeavyEngine, ParserEngine, RapidOcrBackend

    heavy = settings.worker_pool == "HEAVY"
    ocr = RapidOcrBackend() if settings.ocr_enabled else None
    parser = HeavyEngine(ParserEngine(ocr), DoclingConverter()) if heavy else ParserEngine(ocr)
    if page_models is not None:
        parser = PageModelFirst(parser, page_models)

    def read_file(doc: Document) -> bytes:
        return store.path(doc.storage_path).read_bytes()

    gateway = None
    if llm_allowed(settings):  # pragma: no cover - only if the bank ever approves an LLM
        from dps.agent.gateway import from_settings

        gateway = from_settings(settings, database.session_factory)  # every model call goes through the gateway

    ai_covenants = None
    if settings.agreement_ai_check and gateway is not None:  # pragma: no cover - live model
        from dps.extraction.agreement_ai import read_with_skill
        from dps.integrations.spring_tools import SpringTools

        # The agreement's AI reader is the Document Agent running the covenants skill (the same pack, page finder,
        # tools, grounding and library names as ONBOARD), with the Java covenant tools.
        spring = SpringTools.from_settings(settings)

        from dps.skills import load as load_pack

        covenant_budget = load_pack("covenants").limits.get("token_budget")

        def ai_covenants(pages, terms):
            gateway.begin_job(covenant_budget)
            try:
                return read_with_skill(pages, terms, gateway, spring)
            finally:
                gateway.end_job()

    residual = None
    if settings.agreement_ai_check and gateway is not None:  # pragma: no cover - live model
        from dps.skills.covenants import omissions

        def residual(entries, pages):
            # its own budget: the second read of what the first one left unexplained
            gateway.begin_job(covenant_budget)
            try:
                return omissions.read(gateway, entries, pages)
            finally:
                gateway.end_job()

    def learn() -> None:
        # Phase 4: the wordings an administrator promoted (and not retired) apply to every new document.
        from dps import learning

        with database.session_factory() as db:
            learning.apply(db)

    def run_intake(job: Job) -> tuple[str, str, str]:
        deps = IntakeDeps(database.session_factory, read_file, parser, core, settings.review_confidence, heavy,
                          ai_covenants, settings.default_currency, residual)
        learn()
        return _kickoff(AgreementIntakeFlow(deps), job)

    second_reader = None
    if settings.statement_ai_check and gateway is not None:  # pragma: no cover - live model
        from dps.agent.document_agent import completion as agent_completion
        from dps.extraction.statement_ai import check

        completion = agent_completion(gateway, "strong", "statement_lines")  # the Document Agent, statement skill

        def second_reader(text, values, scale=1.0, entity=None):
            return check(text, values, completion, entity or settings.statement_entity, scale)

    def run_statement(job: Job) -> tuple[str, str, str]:
        deps = StatementDeps(database.session_factory, read_file, parser, settings.core_data_source_id, heavy,
                             lambda data, entity=None: read_pdf_statement(
                                 data, ocr, entity=entity or settings.statement_entity),
                             second_reader, settings.statement_entity, settings.fiscal_year_end_month,
                             core.cp_currency, core.fx_rate, settings.default_currency,
                             core.cp_profile, lambda: currency_words(core.country_profiles()))
        learn()
        return _kickoff(StatementProcessingFlow(deps), job)

    return {"AGREEMENT_INTAKE": run_intake, "STATEMENT_PROCESSING": run_statement}


def _kickoff(flow, job: Job) -> tuple[str, str, str]:
    inputs = {"job_id": job.id, "document_id": job.document_id}
    if job.flow_state_id:
        inputs["id"] = job.flow_state_id  # resume the saved run (after review or a heavy hand-off)
    flow.kickoff(inputs=inputs)
    return flow.state.outcome, flow.state.id, flow.state.error


def main() -> None:  # pragma: no cover - process entry point
    from dps.skills.covenants.taxonomy import get as get_taxonomy

    get_taxonomy()  # fail startup before claiming any jobs if the taxonomy is missing or invalid
    settings = get_settings()
    obs.setup("dps-worker-heavy" if settings.worker_pool == "HEAVY" else "dps-worker")
    database = Database(settings.database_url)
    require_snapshot_isolation(database.engine)
    core = CoreClient.from_settings(settings)
    page_models: dict[str, dict] = {}
    ingestor = build_ingestor(settings) if settings.ingest_enabled else None
    worker = Worker(settings, database.session_factory, core,
                    build_runners(settings, database, FileStore(settings.storage_dir), core, page_models),
                    ingestor, page_models, build_analyzer(settings, database),
                    build_pack_runner(settings, database))
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    log.info("DPS worker %s started (pool=%s)", settings.worker_id, settings.worker_pool)
    while not stop["flag"]:
        if not worker.run_once():
            time.sleep(2)


if __name__ == "__main__":  # pragma: no cover
    main()
