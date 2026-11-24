"""Parsing sandbox (Phase 2): untrusted files are inspected and parsed in a child process with limits.

Each child process sets RLIMIT_AS / RLIMIT_CPU / RLIMIT_NOFILE where the OS has
them, cuts itself off from the network (defence in depth; the parser container has no network at all) and
returns only the page model. The caller kills it at the wall-clock limit (60 s per 10 pages, at most 15 min).

Outcomes the worker maps to job codes:
- ``ParseTimeout`` -> PARSE_TIMEOUT
- ``ParserCrash`` -> PARSER_CRASH (a second crash on the same file sends the job to DEAD)
- ``ParserOutOfMemory`` -> OUT_OF_MEMORY
- ``InspectionFailed`` (from the child) -> its own code (TYPE_SPOOFED, UNSAFE_CONTENT ...)
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import socket
import threading
from dataclasses import dataclass
from functools import cache

from dps.ingest.inspect import InspectionFailed

GB = 1024 ** 3


class SandboxError(Exception):
    code = "PARSER_CRASH"


class ParseTimeout(SandboxError):
    code = "PARSE_TIMEOUT"


class ParserCrash(SandboxError):
    code = "PARSER_CRASH"


class ParserOutOfMemory(SandboxError):
    code = "OUT_OF_MEMORY"


@dataclass(frozen=True)
class Limits:
    memory_bytes: int = 2 * GB  # LIGHT pool; HEAVY uses 6 GB
    cpu_seconds: int = 900
    open_files: int = 256
    seconds_per_10_pages: float = 60.0
    max_seconds: float = 900.0
    min_seconds: float = 120.0

    @classmethod
    def for_pool(cls, pool: str) -> Limits:
        """LIGHT: 2 GB, at least 2 min, at most 15. HEAVY (scans, long documents): 6 GB, 3 min per 10 pages, at least 5
        min, at most 60 (a 396-page facility agreement needs more than 15)."""
        if pool == "HEAVY":
            return cls(memory_bytes=6 * GB, seconds_per_10_pages=180.0, min_seconds=300.0, max_seconds=3600.0,
                       cpu_seconds=3600)
        return cls()

    def wall_clock(self, pages: int | None) -> float:
        pages = max(1, pages or 10)
        return min(self.max_seconds, max(self.min_seconds, self.seconds_per_10_pages * pages / 10))


@dataclass(frozen=True)
class ParseRequest:
    data: bytes
    fmt: str
    sha256: str
    version_id: str | None = None
    ocr: bool = True
    heavy: bool = False
    statement: tuple[str, str, str] | None = None  # extension, entity, number style


def _no_network() -> None:
    def refuse(*_a, **_k):
        raise PermissionError("network access is disabled in the parser sandbox")

    class NoSocket(socket.socket):
        # A class, not a function: a module imported later still subclasses it ("class SSLSocket(socket)" in ssl,
        # which Docling imports). A function there raised TypeError, so Docling never ran in the sandbox (bulk run,
        # 22 Nov 2026: every heavy-pool page fell back to the fast reading). Making one still fails.
        def __init__(self, *_a, **_k) -> None:
            refuse()

    socket.socket = NoSocket  # type: ignore[assignment,misc]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def _apply_limits(limits: Limits) -> None:
    # Called in the child before loading OCR or numerical libraries. Native pools otherwise inherit
    # host-sized defaults that can consume the address space reserved for parsing.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    try:
        import resource
    except ImportError:  # Windows development machines: the container enforces the limits
        return
    for name, value in (("RLIMIT_AS", limits.memory_bytes), ("RLIMIT_CPU", limits.cpu_seconds),
                        ("RLIMIT_NOFILE", limits.open_files)):
        res = getattr(resource, name, None)
        if res is not None:
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):  # pragma: no cover - hard limit lower than ours already
                pass


@cache
def _engine(ocr: bool, heavy: bool):
    from dps.parse.engine import DoclingConverter, HeavyEngine, ParserEngine

    fast = ParserEngine(_ocr(ocr))
    return HeavyEngine(fast, DoclingConverter()) if heavy else fast


@cache
def _ocr(enabled: bool):
    from dps.parse.engine import RapidOcrBackend

    return RapidOcrBackend() if enabled else None


def parse_in_process(req: ParseRequest) -> dict:
    """Inspect before parsing, including when the statement reader needs a different entity or number style."""
    from dps.ingest.inspect import MAX_PDF_PAGES, MAX_PDF_PAGES_HEAVY, inspect
    from dps.parse.page_model import build

    report = inspect(req.data, req.fmt, MAX_PDF_PAGES_HEAVY if req.heavy else MAX_PDF_PAGES)
    engine = _engine(req.ocr, req.heavy) if req.fmt in ("PDF", "IMAGE") else None
    try:
        if req.statement:
            from dps.skills.statement_lines.reader import parse_statement

            return parse_statement(req.data, *req.statement, engine=engine, ocr=_ocr(req.ocr) if engine else None,
                                   heavy=req.heavy)
        return build(req.data, req.fmt, req.sha256, req.version_id, report.flags, engine, report.encoding)
    finally:
        if engine is not None and (ocr := _ocr(req.ocr)) is not None:
            ocr.close()  # keep the model, release the file between requests


def _reply(req: ParseRequest, target: str) -> tuple[str, object]:
    try:
        return "ok", _TARGETS[target](req)
    except InspectionFailed as ex:
        return "inspection", (ex.code, str(ex))
    except MemoryError:
        return "oom", "out of memory"
    except BaseException as ex:  # noqa: BLE001 - report every child failure to its supervisor
        return "crash", f"{type(ex).__name__}: {str(ex)[:500]}"


def _warm_child(conn, limits: Limits) -> None:  # pragma: no cover - exercised via the parent
    _apply_limits(limits)
    _no_network()
    with conn:
        while True:
            try:
                req, target = conn.recv()
            except EOFError:
                return
            kind, result = _reply(req, target)
            conn.send_bytes(json.dumps([kind, result]).encode("utf-8"))
            del req, result
            if kind in ("oom", "crash"):
                return


class WarmParser:
    """One reusable child per parser service, recycled after a small batch or any process fault."""

    def __init__(self, limits: Limits, max_requests: int = 16) -> None:
        self.limits, self.max_requests = limits, max_requests
        self._lock = threading.Lock()
        self._process = self._conn = None
        self._requests = 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            if self._process.is_alive():
                self._process.kill()
            self._process.join(5)
            self._process.close()
            self._process = self._conn = None

    def parse(self, req: ParseRequest, pages_hint: int | None = None, target: str = "parse",
              timeout: float | None = None) -> dict:
        wait = timeout if timeout is not None else self.limits.wall_clock(pages_hint)
        with self._lock:
            if self._process is None or not self._process.is_alive():
                self.close()
                ctx = mp.get_context("spawn")
                parent, child = ctx.Pipe()
                process = ctx.Process(target=_warm_child, args=(child, self.limits), daemon=True)
                process.start()
                child.close()
                self._process, self._conn, self._requests = process, parent, 0
            try:
                self._conn.send((req, target))
                if not self._conn.poll(wait):
                    raise ParseTimeout(f"Parsing took longer than {int(wait)} s.")
                kind, payload = json.loads(self._conn.recv_bytes())
            except ParseTimeout:
                self.close()
                raise
            except (EOFError, OSError, ValueError) as ex:
                self.close()
                raise ParserCrash("The parser stopped without a valid reply.") from ex
            self._requests += 1
            if kind in ("oom", "crash") or self._requests >= self.max_requests:
                self.close()
        return _unpack(kind, payload)


def _child(conn, req: ParseRequest, limits: Limits, target: str) -> None:  # pragma: no cover - runs in the child
    _apply_limits(limits)
    _no_network()
    with conn:
        conn.send_bytes(json.dumps(_reply(req, target)).encode("utf-8"))


def _crash_target(req: ParseRequest) -> dict:  # pragma: no cover - used by the sandbox tests
    os._exit(139)


def _hang_target(req: ParseRequest) -> dict:  # pragma: no cover - used by the sandbox tests
    import time

    time.sleep(3600)
    return {}


def _network_target(req: ParseRequest) -> dict:  # pragma: no cover - used by the sandbox tests
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return {"network": "open"}


_TARGETS = {"parse": parse_in_process, "crash": _crash_target, "hang": _hang_target, "network": _network_target}


def run(req: ParseRequest, limits: Limits | None = None, pages_hint: int | None = None,
        target: str = "parse", timeout: float | None = None) -> dict:
    """Runs one request in a fresh spawned child; raises a SandboxError or InspectionFailed on failure."""
    limits = limits or Limits()
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(child, req, limits, target), daemon=True)
    proc.start()
    child.close()
    wait = timeout if timeout is not None else limits.wall_clock(pages_hint)
    try:
        if not parent.poll(wait):
            proc.kill()
            proc.join(5)
            raise ParseTimeout(f"Parsing took longer than {int(wait)} s.")
        try:
            kind, payload = json.loads(parent.recv_bytes())
        except (EOFError, OSError, ValueError) as ex:
            proc.join(5)
            raise ParserCrash(f"The parser stopped (exit code {proc.exitcode}).") from ex
    finally:
        parent.close()
    proc.join(10)
    return _unpack(kind, payload)


def _unpack(kind: str, payload) -> dict:
    if kind == "ok":
        return payload
    if kind == "inspection":
        raise InspectionFailed(*payload)
    if kind == "oom":
        raise ParserOutOfMemory("The parser ran out of memory.")
    raise ParserCrash(payload)
