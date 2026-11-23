"""Ingest step (Phase 2), run by the worker before a job's flow for every job submitted through the job API.

1. Read the version from the DMS store; the SHA-256 is checked while reading (INTEGRITY_MISMATCH).
2. Reuse the page model already built for the same bytes and pipeline version, or
3. inspect and parse in the sandbox (child process, or the parser container over its socket) and save the
   page model write-once under ``derived/<tenant>/<version>/page-model/<pipeline>/page-model.json.gz``,
   listed in ``dps_artifact``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from dps.ingest.inspect import InspectionFailed, declared_format
from dps.ingest.store import LocalStore, StoreError
from dps.models import Artifact, Document, Job
from dps.parse import sandbox
from dps.parse.engine import HEAVY_POOR_SHARE, HEAVY_THRESHOLD

log = logging.getLogger(__name__)
PAGE_MODEL = "PAGE_MODEL"

# Codes that end the job at once (the file will not get better on retry) and ask the DMS to quarantine it.
QUARANTINE = {"TYPE_SPOOFED", "UNSAFE_CONTENT", "MACRO_PRESENT", "ENCRYPTED"}


class IngestFailed(Exception):
    def __init__(self, code: str, message: str, retry: bool = False, heavy: bool = False) -> None:
        super().__init__(message)
        self.code, self.retry, self.heavy = code, retry, heavy


Parse = Callable[[sandbox.ParseRequest, int | None], dict]
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![s\w])")
# The LIGHT pool's longest parse is 900 s at 60 s per 10 pages: more pages than this cannot finish there.
LIGHT_MAX_PAGES = 150


def _poor_share(model: dict) -> float:
    pages = [p for p in model.get("pages") or [] if str(p.get("sourceMethod") or p.get("method")) != "BLANK"]
    return sum(1 for p in pages if float(p.get("quality", 1.0)) < HEAVY_THRESHOLD) / len(pages) if pages else 0.0


def page_hint(data: bytes, fmt: str) -> int | None:
    """How many pages a PDF has, counted from its page objects in the raw bytes, without parsing the file (the parse
    itself runs in the sandbox). None for other formats, or when the pages are hidden in compressed streams."""
    if fmt != "PDF":
        return None
    return len(_PAGE_OBJECT.findall(data)) or None


class Ingestor:
    def __init__(self, store: LocalStore, pipeline_version: str, parse: Parse, ocr: bool = True,
                 heavy: bool = False) -> None:
        self.store, self.pipeline_version, self._parse = store, pipeline_version, parse
        self.ocr, self.heavy = ocr, heavy

    def read(self, doc: Document, staged: bool = False) -> bytes:
        try:
            if staged:
                return self.store.open_staged(doc.storage_path, doc.sha256)
            return self.store.open(doc.object_key or doc.storage_path, doc.sha256)
        except StoreError as ex:
            raise IngestFailed(ex.code, str(ex), retry=ex.code == "STORAGE_UNAVAILABLE") from ex

    def page_model(self, db: Session, job: Job, doc: Document, data: bytes) -> dict:
        try:
            fmt = declared_format(doc.file_name)
        except InspectionFailed as ex:
            raise IngestFailed(ex.code, str(ex)) from ex
        existing = db.scalars(select(Artifact).where(Artifact.tenant_id == _safe(job.tenant_id or "default"),
                                                     Artifact.source_sha256 == doc.sha256,
                                                     Artifact.kind == PAGE_MODEL,
                                                     Artifact.pipeline_version == self.pipeline_version)
                              .order_by(Artifact.created_at)).first()
        if existing is not None:
            try:
                model = json.loads(gzip.decompress(self.store.get_derived(existing.path, existing.sha256)))
                if model.get("format") == fmt:
                    model["documentVersionId"] = job.document_version_id
                    return model
            except StoreError as ex:
                log.warning("Page model %s unreadable (%s); building it again", existing.path, ex.code)
        req = sandbox.ParseRequest(data, fmt, doc.sha256, job.document_version_id, self.ocr, self.heavy)
        hint = page_hint(data, fmt)
        if not self.heavy and hint and hint > LIGHT_MAX_PAGES:  # it cannot finish in the LIGHT pool's time: no try
            raise IngestFailed("LARGE_DOCUMENT", f"{hint} pages: read by the HEAVY pool", heavy=True)
        try:
            model = self._parse(req, hint)  # the time limit scales with the pages (it was always the minimum)
            if not self.heavy and fmt == "PDF" and _poor_share(model) > HEAVY_POOR_SHARE:
                # Many pages are pictures of text (bulk run, 22 Nov 2026: a 140-page agreement with 29 such pages
                # stayed on the LIGHT pool, unread): Docling in the HEAVY pool reads them
                raise IngestFailed("POOR_PAGES", "many pages read poorly: read by the HEAVY pool", heavy=True)
        except InspectionFailed as ex:
            raise IngestFailed(ex.code, str(ex)) from ex
        except sandbox.SandboxError as ex:
            # Too slow or too big for the LIGHT pool (usually a scan): the HEAVY pool gets it with longer limits.
            to_heavy = not self.heavy and ex.code in ("PARSE_TIMEOUT", "OUT_OF_MEMORY")
            raise IngestFailed(ex.code, str(ex), retry=ex.code == "PARSER_CRASH", heavy=to_heavy) from ex
        self._save(db, job, doc, model)
        return model

    def _save(self, db: Session, job: Job, doc: Document, model: dict) -> None:
        version = _safe(job.document_version_id or doc.id)
        tenant = _safe(job.tenant_id or "default")
        path = LocalStore.derived_path(tenant, version, "page-model", _safe(self.pipeline_version),
                                       "page-model.json.gz")
        if self.store.has_derived(path):
            return
        raw = gzip.compress(json.dumps(model, separators=(",", ":")).encode("utf-8"), mtime=0)
        try:
            record = self.store.put_derived(tenant, version, path, raw, PAGE_MODEL)
        except StoreError as ex:
            if ex.code == "ARTIFACT_EXISTS":
                return
            raise IngestFailed("STORAGE_UNAVAILABLE", str(ex), retry=True) from ex
        db.add(Artifact(tenant_id=tenant, version_id=version, kind=PAGE_MODEL, path=record.path,
                        sha256=record.sha256, bytes=record.bytes, source_sha256=doc.sha256,
                        pipeline_version=self.pipeline_version))
        db.commit()


def _safe(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:128]
    return cleaned or hashlib.sha256(value.encode()).hexdigest()[:32]


def sandbox_parser(pool: str, socket_path: str = "") -> Parse:
    """The parse function the worker uses: the parser container's socket when configured, else a child process."""
    if socket_path:
        from dps.parse.server import SocketParser

        client = SocketParser(socket_path)
        return lambda req, hint: client.parse(req, hint)
    limits = sandbox.Limits.for_pool(pool)
    return lambda req, hint: sandbox.run(req, limits, hint)
