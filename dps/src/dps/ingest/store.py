"""DPS's one door to the DMS store (Phase 2): read committed and staged files, write derived artefacts.

The pipeline only sees ``DocumentStore``; the backing store can change (VM folders today, an S3-compatible
store later) without touching it.

- ``open`` / ``open_staged`` stream the file, hash it while reading and refuse a mismatch (INTEGRITY_MISMATCH).
- ``put_derived`` is write-once: an existing path is refused, and every artefact is listed in ``dps_artifact``.
- ``get_derived`` reads an artefact back and re-checks its hash.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CHUNK = 1024 * 1024
_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class StoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DerivedRecord:
    tenant_id: str
    version_id: str
    kind: str
    path: str
    sha256: str
    bytes: int


class DocumentStore(Protocol):
    def open(self, object_key: str, sha256: str) -> bytes: ...
    def open_staged(self, staging_ref: str, sha256: str) -> bytes: ...
    def put_derived(self, tenant: str, version: str, path: str, data: bytes, kind: str) -> DerivedRecord: ...
    def get_derived(self, path: str, sha256: str) -> bytes: ...
    def has_derived(self, path: str) -> bool: ...


def _inside(root: Path, rel: str) -> Path:
    if not rel or rel.startswith(("/", "\\")) or ":" in rel or ".." in Path(rel).parts:
        raise StoreError("INVALID_INPUT", "Not a valid storage key.")
    resolved = (root / rel).resolve()
    if root.resolve() not in resolved.parents:
        raise StoreError("INVALID_INPUT", "Not a valid storage key.")
    return resolved


class LocalStore:
    """VM folders: ``blobs`` (committed versions) and ``staging`` read-only, ``derived`` read-write."""

    def __init__(self, blobs: Path, staging: Path, derived: Path, max_bytes: int = 100 * 1024 * 1024,
                 on_derived: Callable[[DerivedRecord], None] | None = None) -> None:
        self.blobs, self.staging, self.derived = Path(blobs), Path(staging), Path(derived)
        self.max_bytes = max_bytes
        self.on_derived = on_derived

    def _read(self, root: Path, rel: str, sha256: str) -> bytes:
        path = _inside(root, rel)
        if not path.is_file():
            raise StoreError("NOT_FOUND", "No file is stored under that key.")
        digest = hashlib.sha256()
        parts: list[bytes] = []
        size = 0
        try:
            with path.open("rb") as f:
                while chunk := f.read(CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise StoreError("TOO_LARGE", f"The file is larger than {self.max_bytes} bytes.")
                    digest.update(chunk)
                    parts.append(chunk)
        except OSError as ex:
            raise StoreError("STORAGE_UNAVAILABLE", f"Could not read the file: {type(ex).__name__}") from ex
        if digest.hexdigest() != sha256:
            raise StoreError("INTEGRITY_MISMATCH", "The stored file does not match its SHA-256.")
        return b"".join(parts)

    def open(self, object_key: str, sha256: str) -> bytes:
        return self._read(self.blobs, object_key, sha256)

    def open_staged(self, staging_ref: str, sha256: str) -> bytes:
        return self._read(self.staging, staging_ref, sha256)

    @staticmethod
    def derived_path(tenant: str, version: str, *parts: str) -> str:
        for p in (tenant, version, *parts):
            if not _SAFE_PART.match(p) or set(p) == {"."}:
                raise StoreError("INVALID_INPUT", f"Unsafe path part: {p[:40]!r}")
        return "/".join((tenant, version, *parts))

    def has_derived(self, path: str) -> bool:
        return _inside(self.derived, path).is_file()

    def put_derived(self, tenant: str, version: str, path: str, data: bytes, kind: str) -> DerivedRecord:
        if not path.startswith(f"{tenant}/{version}/"):
            raise StoreError("INVALID_INPUT", "A derived path must sit under <tenant>/<version>/.")
        target = _inside(self.derived, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o440)
        except FileExistsError as ex:
            raise StoreError("ARTIFACT_EXISTS", f"Derived artefact already exists: {path}") from ex
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        record = DerivedRecord(tenant, version, kind, path, hashlib.sha256(data).hexdigest(), len(data))
        if self.on_derived:
            self.on_derived(record)
        return record

    def get_derived(self, path: str, sha256: str) -> bytes:
        return self._read(self.derived, path, sha256)
