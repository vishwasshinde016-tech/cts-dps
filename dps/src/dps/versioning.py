"""The pipeline version (full review 19 Nov 2026, G13).

Jobs are de-duplicated and page models reused by ``pipeline_version``. A fixed value ("1") meant that after a rule,
prompt, taxonomy, parser or model-route change, the same document returned the old job as a duplicate and the old
page model was reused. With ``DPS_PIPELINE_VERSION=auto`` (the default) the version is computed at start-up from
everything that changes what DPS reads and extracts:

- the DPS package version;
- the source of the reading and extracting code and the skill packs (prompts, schemas, rules, taxonomy);
- the parser and model library versions;
- the model routes (file content, or the single model name).

Any change gives a new version, so new jobs run fresh while old jobs and their runs stay as they were.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import metadata
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
# What decides the result: reading, classification, extraction, skills, the agent, scoring and the result builder.
SOURCES = ("analyze", "agent", "extraction", "ingest", "parse", "skills", "scoring")
FILES = ("catalog.py", "language.py", "money.py", "profiles.py", "results.py")
LIBRARIES = ("pypdf", "pdfplumber", "pypdfium2", "pikepdf", "rapidocr_onnxruntime", "docling", "openpyxl", "xlrd",
             "python-docx", "crewai")
SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json"}


def _library(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "-"


@lru_cache(maxsize=8)
def compute(routes_file: str = "", model: str = "") -> str:
    from dps import __version__

    digest = hashlib.sha256()
    paths = [p for d in SOURCES for p in (PACKAGE / d).rglob("*") if p.suffix in SUFFIXES and p.is_file()]
    paths += [PACKAGE / f for f in FILES if (PACKAGE / f).is_file()]
    for path in sorted(paths, key=lambda p: p.relative_to(PACKAGE).as_posix()):
        digest.update(path.relative_to(PACKAGE).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))  # same code on Windows and Linux, same version
    for lib in LIBRARIES:
        digest.update(f"{lib}={_library(lib)};".encode())
    routes = Path(routes_file) if routes_file else None
    digest.update(routes.read_bytes() if routes is not None and routes.is_file() else model.encode())
    return f"{__version__}+{digest.hexdigest()[:12]}"


def resolve(value: str, routes_file: str = "", model: str = "") -> str:
    """``auto`` (or empty) becomes the computed version; any other value is kept (pinned by operations)."""
    return compute(routes_file or "", model or "") if value in ("", "auto") else value
