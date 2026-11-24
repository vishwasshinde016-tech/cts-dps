"""Java business rules called by the DPS agent as batched tool calls (contracts/spring-tools.yaml).

Decision of 17 Nov 2026 (scaling review): parsing, OCR and the AI agent stay in Python; formula validation,
master-data lookup and the covenant library stay in Java in the bank's Spring app. Every call here is:

- batched: one HTTP call per document (up to ``MAX_BATCH`` items), never one per field;
- bounded: the client's timeout, at most ``retries`` retries on timeout / 5xx with back-off, and a circuit breaker;
- degrading: when Spring cannot answer, each item comes back ``available=False`` with ``SPRING_UNAVAILABLE`` so
  the caller sends that field to review and the job carries on. A tool outage never fails a job.

Until the Spring team ships ``/api/v1/dps-tools/formulas/validate``, a 404 on it falls back to the existing
single-item ``/api/v1/formulas/validate`` with at most ``FALLBACK_PARALLEL`` calls in flight.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field

from dps.integrations.spring import SpringClient, SpringUnavailable

log = logging.getLogger(__name__)

UNAVAILABLE = "SPRING_UNAVAILABLE"
MAX_BATCH = 200
FALLBACK_PARALLEL = 4
LOOKUP_KINDS = frozenset({"FACILITY_ID", "LOAN_ACCOUNT", "CIN", "PAN", "GSTIN", "LEI", "BORROWER_NAME"})
response_version: ContextVar[str | None] = ContextVar("spring_tool_response_version", default=None)


class CircuitOpen(SpringUnavailable):
    """The breaker is open; no call was made."""


class CircuitBreaker:
    """Opens after ``threshold`` failures inside ``window`` seconds and stays open for ``cooldown`` seconds.
    After the cool-down one trial call is let through (half-open); success closes it, failure re-opens it."""

    def __init__(self, threshold: int = 5, window: float = 60.0, cooldown: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.window, self.cooldown, self._clock = threshold, window, cooldown, clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self._opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self._clock() - self._opened_at >= self.cooldown else "OPEN"

    def allow(self) -> bool:
        with self._lock:
            return self._state() != "OPEN"

    def success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._opened_at is not None:  # a failed half-open trial re-opens the breaker
                self._opened_at = now
                return
            self._failures.append(now)
            while self._failures and now - self._failures[0] > self.window:
                self._failures.popleft()
            if len(self._failures) >= self.threshold:
                self._opened_at = now
                log.warning("spring tools circuit opened after %d failures", len(self._failures))


@dataclass(frozen=True)
class FormulaCheck:
    id: str
    available: bool
    valid: bool | None = None
    errors: tuple[dict, ...] = ()
    code: str | None = None  # SPRING_UNAVAILABLE when not available


@dataclass(frozen=True)
class LookupMatch:
    id: str
    available: bool
    status: str | None = None  # EXACT | STRONG | FUZZY | CONFLICT | NONE
    candidates: tuple[dict, ...] = ()
    code: str | None = None


@dataclass
class _LibraryCache:
    etag: str | None = None
    version: str | None = None
    items: list[dict] = field(default_factory=list)


def _refused(resp, what: str) -> None:
    """Any 4xx from the bank app (401 wrong token, 403, 400 a payload it does not take): the tool is unavailable for
    this job (a warning on the result), never an error that kills the job (CTS review, 23 Nov 2026: one wrong token
    made every job with covenants DEAD after three retries)."""
    if resp.status_code >= 400:
        log.warning("%s refused by the bank app: HTTP %s", what, resp.status_code)
        raise SpringUnavailable(f"{what}: HTTP {resp.status_code}")


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SpringTools:
    def __init__(self, core: SpringClient, breaker: CircuitBreaker | None = None, retries: int = 2,
                 backoff_seconds: float = 0.5, sleep: Callable[[float], None] = time.sleep) -> None:
        self._core = core
        self.breaker = breaker or CircuitBreaker()
        self._retries, self._backoff, self._sleep = retries, backoff_seconds, sleep
        self._batch_formulas = True  # flips to False after the first 404 (endpoint not deployed yet)
        self._library = _LibraryCache()

    @classmethod
    def from_settings(cls, settings) -> SpringTools:
        return cls(SpringClient.from_settings(settings))

    # -- transport --------------------------------------------------------------------------------------------

    def _call(self, method: str, path: str, correlation_id: str | None = None, **kw):
        """One bounded call: breaker check, retries with back-off on SpringUnavailable, breaker bookkeeping."""
        headers = dict(kw.pop("headers", None) or {})
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id[:128]
        last: SpringUnavailable | None = None
        for attempt in range(self._retries + 1):
            if not self.breaker.allow():
                raise CircuitOpen(f"{method} {path}: circuit open")
            try:
                resp = self._core._request(method, path, headers=headers or None, **kw)
            except SpringUnavailable as exc:
                self.breaker.failure()
                last = exc
                if attempt < self._retries:
                    self._sleep(self._backoff * (2 ** attempt))
                continue
            self.breaker.success()
            response_version.set((resp.headers.get("X-Spring-Version") or "")[:128] or None)
            return resp
        raise last or SpringUnavailable(f"{method} {path}")

    # -- formulas ---------------------------------------------------------------------------------------------

    def validate_formulas(self, items: Sequence[tuple[str, str]], correlation_id: str | None = None,
                          require_boolean: bool = True) -> dict[str, FormulaCheck]:
        """``items`` is ``[(field_id, expression), ...]``; returns one FormulaCheck per id."""
        out: dict[str, FormulaCheck] = {}
        for chunk in _chunks(list(items), MAX_BATCH):
            try:
                out.update(self._validate_chunk(chunk, correlation_id, require_boolean))
            except SpringUnavailable as exc:
                log.warning("formula validation unavailable for %d items: %s", len(chunk), exc)
                out.update({fid: FormulaCheck(fid, False, code=UNAVAILABLE) for fid, _ in chunk})
        return out

    def _validate_chunk(self, chunk, correlation_id, require_boolean) -> dict[str, FormulaCheck]:
        if self._batch_formulas:
            body = {"items": [{"id": fid, "expression": expr, "requireBoolean": require_boolean}
                              for fid, expr in chunk]}
            resp = self._call("POST", "/api/v1/dps-tools/formulas/validate", correlation_id, json=body)
            if resp.status_code == 404:
                log.info("batch formula tool not deployed; using the single-item endpoint")
                self._batch_formulas = False
            else:
                _refused(resp, "formula validation")
                return {str(r["id"]): FormulaCheck(str(r["id"]), True, bool(r.get("valid")),
                                                   tuple(r.get("errors") or ()))
                        for r in resp.json().get("items", [])}
        return self._validate_single(chunk, correlation_id, require_boolean)

    def _validate_single(self, chunk, correlation_id, require_boolean) -> dict[str, FormulaCheck]:
        def one(item: tuple[str, str]) -> FormulaCheck:
            fid, expr = item
            try:
                resp = self._call("POST", "/api/v1/formulas/validate", correlation_id,
                                  json={"expression": expr, "requireBoolean": require_boolean})
                _refused(resp, "formula validation")
            except SpringUnavailable:
                return FormulaCheck(fid, False, code=UNAVAILABLE)
            body = resp.json()
            return FormulaCheck(fid, True, bool(body.get("valid")), tuple(body.get("errors") or ()))

        with ThreadPoolExecutor(max_workers=FALLBACK_PARALLEL) as pool:
            return {r.id: r for r in pool.map(one, chunk)}

    # -- lookups ----------------------------------------------------------------------------------------------

    def lookup_identifiers(self, items: Sequence[tuple[str, str, str]], context: dict | None = None,
                           correlation_id: str | None = None) -> dict[str, LookupMatch]:
        """``items`` is ``[(id, kind, value), ...]`` with kind in LOOKUP_KINDS."""
        bad = {k for _, k, _ in items} - LOOKUP_KINDS
        if bad:
            raise ValueError(f"unknown identifier kinds: {sorted(bad)}")
        out: dict[str, LookupMatch] = {}
        for chunk in _chunks(list(items), MAX_BATCH):
            body: dict = {"items": [{"id": i, "kind": k, "value": v[:300]} for i, k, v in chunk]}
            if context:
                body["context"] = context
            try:
                resp = self._call("POST", "/api/v1/dps-tools/lookups", correlation_id, json=body)
                if resp.status_code == 404:
                    raise SpringUnavailable("lookup tool not deployed")
                _refused(resp, "identifier lookup")
            except SpringUnavailable as exc:
                log.warning("identifier lookup unavailable for %d items: %s", len(chunk), exc)
                out.update({i: LookupMatch(i, False, code=UNAVAILABLE) for i, _, _ in chunk})
                continue
            for r in resp.json().get("items", []):
                out[str(r["id"])] = LookupMatch(str(r["id"]), True, r.get("status"), tuple(r.get("candidates") or ()))
        return out

    # -- covenant library -------------------------------------------------------------------------------------

    def covenant_library(self) -> tuple[list[dict], bool]:
        """(items, fresh). ``fresh`` is False when Spring did not answer and the last cached copy is returned."""
        headers = {"If-None-Match": self._library.etag} if self._library.etag else None
        try:
            resp = self._call("GET", "/api/v1/dps-tools/covenant-library", headers=headers)
        except SpringUnavailable as exc:
            log.warning("covenant library unavailable, using cached copy: %s", exc)
            return list(self._library.items), False
        if resp.status_code == 304:
            return list(self._library.items), True
        if resp.status_code == 404:
            return list(self._library.items), False
        try:
            _refused(resp, "covenant library")
            body = resp.json()
        except (SpringUnavailable, ValueError) as exc:  # refused or not JSON: the cached copy, a warning, no crash
            log.warning("covenant library not usable, using cached copy: %s", exc)
            return list(self._library.items), False
        items = body.get("items") if isinstance(body, dict) else None
        self._library = _LibraryCache(resp.headers.get("ETag"), body.get("version") if isinstance(body, dict) else None,
                                      [i for i in items or [] if isinstance(i, dict) and i.get("id")
                                       and i.get("active", True) is not False])
        return list(self._library.items), True
