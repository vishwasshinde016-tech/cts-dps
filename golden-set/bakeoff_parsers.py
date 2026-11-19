"""Phase 2 parser bake-off and parity check on the golden set.

    python golden-set/bakeoff_parsers.py [--ocr] [--out golden-set/bakeoff-phase2.json]

For every document in golden-set/documents it reports:
- inspection outcome (every golden file must pass);
- parity: the page model's page text equals today's ParserEngine text (the flows' input is unchanged);
- per-library native text speed and size: pypdf, pdfplumber, pypdfium2 (seconds per page, characters);
- with --ocr, RapidOCR seconds per page on pages without a text layer.
PaddleOCR, Tesseract, Marker and MinerU are not installed in this repository; their rows are added when the
bank's scanned set is run on the heavy host (see the decision register in docs/cts-dps/design.md).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "dps" / "src"))

from dps.ingest.inspect import InspectionFailed, declared_format, inspect  # noqa: E402
from dps.parse.page_model import build, to_pages  # noqa: E402
from dps.parse.engine import ParserEngine, RapidOcrBackend  # noqa: E402


def _time(fn) -> tuple[float, object]:
    start = time.perf_counter()
    out = fn()
    return time.perf_counter() - start, out


def native_libraries(data: bytes) -> dict[str, tuple[float, int]]:
    import pdfplumber
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    def with_pypdf():
        return "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)

    def with_plumber():
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "".join(p.extract_text() or "" for p in pdf.pages)

    def with_pdfium():
        pdf = pdfium.PdfDocument(data)
        return "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))

    out = {}
    for name, fn in (("pypdf", with_pypdf), ("pdfplumber", with_plumber), ("pypdfium2", with_pdfium)):
        seconds, text = _time(fn)
        out[name] = (seconds, len(text))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "bakeoff-phase2.json"))
    args = ap.parse_args()
    engine = ParserEngine(RapidOcrBackend() if args.ocr else None)
    rows, failures = [], []
    for path in sorted((ROOT / "documents").glob("*.pdf")):
        data = path.read_bytes()
        try:
            report = inspect(data, declared_format(path.name))
        except InspectionFailed as ex:
            failures.append({"file": path.name, "code": ex.code})
            continue
        start = time.perf_counter()
        model = build(data, "PDF", hashlib.sha256(data).hexdigest(), path.stem, report.flags, engine)
        seconds = time.perf_counter() - start
        parity = [p.text for p in to_pages(model)] == [p.text for p in engine.parse(data)]
        libs = native_libraries(data)
        pages = model["pageCount"] or 1
        rows.append({"file": path.name, "pages": model["pageCount"], "flags": model["flags"],
                     "segments": len(model["segments"]), "parity": parity,
                     "pageModelSecondsPerPage": round(seconds / pages, 4),
                     "blocksWithBox": sum("bbox" in b for p in model["pages"] for b in p["blocks"]),
                     "blocks": sum(len(p["blocks"]) for p in model["pages"]),
                     "libraries": {k: {"secondsPerPage": round(v[0] / pages, 4), "chars": v[1]}
                                   for k, v in libs.items()}})
    summary = {
        "documents": len(rows) + len(failures), "inspectionFailures": failures,
        "parity": sum(r["parity"] for r in rows), "parityOf": len(rows),
        "medianPageModelSecondsPerPage": (statistics.median(r["pageModelSecondsPerPage"] for r in rows)
                                          if rows else None),
        "boxCoverage": round(sum(r["blocksWithBox"] for r in rows) / max(1, sum(r["blocks"] for r in rows)), 3),
        "medianSecondsPerPage": {lib: statistics.median(r["libraries"][lib]["secondsPerPage"] for r in rows)
                                 for lib in ("pypdf", "pdfplumber", "pypdfium2")} if rows else {},
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "documents": rows}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if not failures and summary["parity"] == summary["parityOf"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
