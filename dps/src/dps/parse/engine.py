"""ParserEngine (03-flows-design.md §8): pypdf native text first, RapidOCR only for pages without usable
text, Docling as an opt-in heavy fallback. Azure AI Document Intelligence is not used.

Each page comes back with the method that produced it and a quality score, so Flow A/B can route a
document to the heavy worker pool when the fast parse is not good enough.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

log = logging.getLogger(__name__)

# A page with fewer printable word characters than this is treated as having no text layer.
MIN_TEXT_CHARS = 40
# A page below this quality is "poor"; more than HEAVY_POOR_SHARE poor pages sends the document to the
# heavy (Docling) pool. A few blank pages (signature, cover) are normal and do not.
HEAVY_THRESHOLD = 0.5
HEAVY_POOR_SHARE = 0.25
# An OCR'd page that yields fewer word characters than this is poor whatever OCR's confidence: a real
# agreement page has hundreds, and a few confident fragments of a blurred scan are not readable text.
MIN_OCR_CHARS = 80
POOR_OCR_QUALITY = 0.3
# A page without text whose images are all smaller than this on a side (a page-break marker, a bullet icon) and
# whose drawing instructions are this short is blank: OCR or Docling cannot find text on it. Blank pages
# printed from web filings were a third of some agreements and sent all of them to the heavy pool.
MIN_PICTURE_SIDE = 100
MAX_BLANK_CONTENT_BYTES = 2000
# The heavy pool sends Docling the pages the fast parse could not read, this many at a time, so memory stays
# flat on long documents (a whole 595-page agreement in one call ran out of memory on a 7.6 GB host).
DOCLING_CHUNK_PAGES = 10


@dataclass
class Page:
    no: int
    text: str
    method: str  # TEXT | OCR | DOCLING | NONE | BLANK
    quality: float

    def as_dict(self) -> dict:
        return asdict(self)


class ParseError(Exception):
    """The file is not a readable PDF."""


class OcrBackend(Protocol):
    def ocr_page(self, pdf_bytes: bytes, page_index: int) -> tuple[str, float]:
        """Returns (text, mean confidence 0..1) for one page."""


def text_quality(text: str) -> float:
    """Share of word characters among non-space characters, damped for very short pages (0..1)."""
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return 0.0
    words = len(re.findall(r"[A-Za-z0-9]", stripped))
    ratio = words / len(stripped)
    length_factor = min(1.0, len(stripped) / 200)
    return round(ratio * (0.5 + 0.5 * length_factor), 3)


# RapidOCR's own text model is for Chinese: on English pages it runs the words together ("TheTrusteeishereby
# appointed", "SRILANKANRUPEES"), so no rule could match a scanned page (bulk run, 22 Nov 2026: one page gave 152
# words, 39 run together; PP-OCRv6 gave 450 words, none run together, and was faster). The images carry PP-OCRv6.
REC_MODEL_NAME = "PP-OCRv6_rec_small.onnx"
REC_MODEL_PLACES = ("/opt/ocr-models",)


def rec_model() -> Path | None:
    """The PP-OCRv6 text model: DPS_OCR_REC_MODEL, the image's folder, or Docling's model folder; None if absent."""
    import os

    named = os.environ.get("DPS_OCR_REC_MODEL")
    places = [Path(named)] if named else []
    places += [Path(p) / REC_MODEL_NAME for p in REC_MODEL_PLACES]
    docling = os.environ.get("DPS_DOCLING_ARTIFACTS") or Path.home() / ".cache" / "docling" / "models"
    places.append(Path(docling) / "RapidOcr" / "onnx" / "PP-OCRv6" / "rec" / REC_MODEL_NAME)
    found = next((p for p in places if p.is_file()), None)
    if found is None:
        log.warning("OCR text model %s not found: RapidOCR's own model runs English words together", REC_MODEL_NAME)
    return found


class RapidOcrBackend:
    """RapidOCR (ONNX runtime, CPU). Imported lazily so the API process does not load the model."""

    def __init__(self, dpi: int = 200) -> None:
        self._dpi = dpi
        self._engine = None
        # The open PDF of the document being read: opened once, not once per page (a 300-page scan used to be
        # parsed 300 times and never closed). The bytes are kept so their id cannot be reused while cached.
        self._doc = None
        self._doc_key: tuple[int, int] | None = None
        self._doc_bytes: bytes | None = None

    def _document(self, pdf_bytes: bytes):  # pragma: no cover - exercised where pypdfium2 is installed
        import pypdfium2 as pdfium

        key = (id(pdf_bytes), len(pdf_bytes))
        if self._doc is None or self._doc_key != key:
            self.close()
            self._doc, self._doc_key, self._doc_bytes = pdfium.PdfDocument(pdf_bytes), key, pdf_bytes
        return self._doc

    def close(self) -> None:
        """Releases the cached PDF (after a document, or when the next one arrives)."""
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception as ex:  # noqa: BLE001 - closing is best effort; the document is dropped anyway
                log.debug("Closing the OCR document failed: %s", type(ex).__name__)
        self._doc, self._doc_key, self._doc_bytes = None, None, None

    def _ensure(self):  # pragma: no cover - exercised only where the optional OCR stack is installed
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            model = rec_model()
            self._engine = RapidOCR(rec_model_path=str(model)) if model else RapidOCR()
        return self._engine

    def ocr_page(self, pdf_bytes: bytes, page_index: int) -> tuple[str, float]:  # pragma: no cover
        result = self.ocr_boxes(pdf_bytes, page_index, self._dpi)
        if not result:
            return "", 0.0
        lines = [r[1] for r in result]
        confidence = sum(float(r[2]) for r in result) / len(result)
        return "\n".join(lines), confidence

    def ocr_boxes(self, pdf_bytes: bytes, page_index: int, dpi: int) -> list:  # pragma: no cover
        """RapidOCR's raw result for one page: [(four corner points in pixels, text, confidence), ...]."""
        page = self._document(pdf_bytes)[page_index]
        try:
            image = page.render(scale=dpi / 72).to_pil()
        finally:
            page.close()
        result, _ = self._ensure()(image)
        return list(result or [])


class ParserEngine:
    """Fast path used by both flows. ``ocr`` may be None (no OCR available): such pages come back empty.

    A page whose text layer reads poorly keeps it here: on 72 real documents such pages were tables of contents and
    covers, and reading them by OCR as well changed one page and no finding, for minutes of OCR (bulk run, 22 Nov
    2026). A document with many of them goes to the heavy pool (ingest POOR_PAGES); there OCR reads what Docling
    cannot (``ocr_poor_pages``)."""

    def __init__(self, ocr: OcrBackend | None = None) -> None:
        self._ocr = ocr

    def parse(self, pdf_bytes: bytes) -> list[Page]:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            raw_pages = [(p.extract_text() or "", p) for p in reader.pages]
        except Exception as ex:  # pypdf raises many types for corrupt input
            raise ParseError(f"Unreadable PDF: {type(ex).__name__}") from ex

        pages: list[Page] = []
        for i, (text, page) in enumerate(raw_pages):
            if len(re.findall(r"\w", text)) >= MIN_TEXT_CHARS:
                pages.append(Page(i + 1, text, "TEXT", text_quality(text)))
            elif _is_blank(page):
                pages.append(Page(i + 1, text, "BLANK", 0.0))
            else:
                pages.append(self._ocr_or_empty(pdf_bytes, i, text))
        close = getattr(self._ocr, "close", None)
        if callable(close):
            close()
        return pages

    def ocr_poor_pages(self, pdf_bytes: bytes, pages: list[Page], numbers: list[int]) -> list[Page]:
        """These pages read by OCR as well; the OCR reading replaces a page only when it reads better."""
        if self._ocr is None or not numbers:
            return pages
        out = list(pages)
        for no in numbers:
            current = out[no - 1]
            read = self._ocr_or_empty(pdf_bytes, no - 1, current.text)
            if read.method == "OCR" and read.quality > current.quality and \
                    len(re.findall(r"\w", read.text)) > len(re.findall(r"\w", current.text)):
                out[no - 1] = read
        return out

    def _ocr_or_empty(self, pdf_bytes: bytes, index: int, native: str) -> Page:
        if self._ocr is None:
            return Page(index + 1, native, "NONE", text_quality(native))
        try:
            text, confidence = self._ocr.ocr_page(pdf_bytes, index)
        except Exception as ex:
            log.warning("OCR failed on page %d: %s", index + 1, type(ex).__name__)
            return Page(index + 1, native, "NONE", 0.0)
        quality = round(min(confidence, text_quality(text) or confidence), 3)
        if len(re.findall(r"\w", text)) < MIN_OCR_CHARS:
            quality = min(quality, POOR_OCR_QUALITY)
        return Page(index + 1, text, "OCR", quality)


def _is_blank(page, depth: int = 0) -> bool:
    """No picture that could hold text and almost no drawing: nothing for OCR to read. Unsure means not blank."""
    try:
        if depth == 0:
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_BLANK_CONTENT_BYTES:
                return False  # vector drawing or an inline image
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        for ref in xobjects.values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                if min(int(obj.get("/Width", 0)), int(obj.get("/Height", 0))) >= MIN_PICTURE_SIDE:
                    return False
            elif obj.get("/Subtype") == "/Form" and (depth >= 3 or not _is_blank(obj, depth + 1)):
                return False
    except Exception:
        return False
    return True


def needs_heavy(pages: list[Page]) -> bool:
    """True when the fast parse left too many pages unusable: route to the Docling pool. Blank pages do not count."""
    readable = [p for p in pages if p.method != "BLANK"]
    if not readable:
        return True
    poor = sum(1 for p in readable if p.quality < HEAVY_THRESHOLD)
    return poor / len(readable) > HEAVY_POOR_SHARE


def page_runs(numbers: list[int], size: int) -> list[tuple[int, int]]:
    """Consecutive page numbers grouped into (first, last) runs of at most ``size`` pages."""
    runs: list[tuple[int, int]] = []
    for n in sorted(numbers):
        if runs and n == runs[-1][1] + 1 and n - runs[-1][0] < size:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


class HeavyEngine:
    """dps-worker-heavy: the fast parse first, then Docling only for the pages it could not read, a run of pages
    at a time. Pages the fast parse read well keep its text (the same text the standard pool reads).

    ``convert(pdf_bytes, first, last)`` returns {page number: text}; a run it fails on keeps the fast parse's page.
    """

    def __init__(self, fast: ParserEngine, convert, chunk: int = DOCLING_CHUNK_PAGES) -> None:
        self._fast, self._convert, self._chunk = fast, convert, chunk

    def parse(self, pdf_bytes: bytes) -> list[Page]:
        pages = self._fast.parse(pdf_bytes)
        poor = [p.no for p in pages if p.method != "BLANK" and p.quality < HEAVY_THRESHOLD]
        runs = page_runs(poor, self._chunk)
        failed: list[int] = []
        for i, (first, last) in enumerate(runs):
            log.info("Docling pages %d-%d (run %d of %d)", first, last, i + 1, len(runs))
            try:
                texts = self._convert(pdf_bytes, first, last)
            except Exception as ex:
                log.warning("Docling failed on pages %d-%d: %s", first, last, type(ex).__name__)
                failed += [no for no in poor if first <= no <= last]
                continue
            for no, text in texts.items():
                if first <= no <= last:
                    pages[no - 1] = Page(no, text, "DOCLING", max(text_quality(text), HEAVY_THRESHOLD))
        # Where Docling failed (not installed, models missing, a crash), OCR reads the page rather than leaving it
        # unread (bulk run, 22 Nov 2026: 44 runs failed and their pages stayed unread)
        fallback = getattr(self._fast, "ocr_poor_pages", None)
        return fallback(pdf_bytes, pages, failed) if fallback and failed else pages


def pdf_slice(pdf_bytes: bytes, first: int, last: int) -> bytes:
    """Pages ``first``..``last`` (1-based, inclusive) as a PDF of their own, so a slow reader loads only them."""
    from pypdf import PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for i in range(max(first, 1) - 1, min(last, len(reader.pages))):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class DoclingConverter:  # pragma: no cover - heavy optional dependency, run only by dps-worker-heavy
    """Layout-aware reader, about 45-55x slower than the fast path (03-flows-design §8). One converter is kept,
    so its models load once per worker rather than once per document."""

    def __init__(self) -> None:
        self._converter = None

    @staticmethod
    def _make():
        """The converter on the models baked into the image (DPS_DOCLING_ARTIFACTS, default ~/.cache/docling/models):
        the parse sandbox has no network, so Docling must never download at first use (bulk run, 22 Nov 2026: every
        Docling run failed). Its OCR is RapidOCR, the engine of DPS's fast path."""
        import os

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        folder = Path(os.environ.get("DPS_DOCLING_ARTIFACTS") or Path.home() / ".cache" / "docling" / "models")
        options = PdfPipelineOptions(ocr_options=RapidOcrOptions())
        if folder.is_dir():
            options.artifacts_path = str(folder)
        else:
            log.warning("Docling models folder %s not found: Docling would try to download them", folder)
        return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    def __call__(self, pdf_bytes: bytes, first: int, last: int) -> dict[int, str]:
        from docling.datamodel.base_models import DocumentStream

        if self._converter is None:
            self._converter = self._make()
        try:  # only the run's pages: a 500-page agreement no longer loads in full for every 10-page run
            part = pdf_slice(pdf_bytes, first, last)
            result = self._converter.convert(DocumentStream(name="part.pdf", stream=io.BytesIO(part)))
            doc = result.document
            return {first + no - 1: doc.export_to_markdown(page_no=no) for no in sorted(doc.pages)}
        except Exception as ex:  # noqa: BLE001 - a PDF pypdf cannot split: the whole file, as before
            log.info("Docling slice failed (%s); reading pages %d-%d of the whole file", type(ex).__name__, first, last)
        result = self._converter.convert(DocumentStream(name="doc.pdf", stream=io.BytesIO(pdf_bytes)),
                                         page_range=(first, last))
        doc = result.document
        return {no: doc.export_to_markdown(page_no=no) for no in sorted(doc.pages)}


class DoclingEngine:  # pragma: no cover - heavy optional dependency, used by the benchmarks
    """Docling on every page, a run of pages at a time (the benchmarks compare it with the other readers)."""

    def __init__(self) -> None:
        self._convert = DoclingConverter()

    def parse(self, pdf_bytes: bytes) -> list[Page]:
        count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
        pages: list[Page] = []
        for first, last in page_runs(list(range(1, count + 1)), DOCLING_CHUNK_PAGES):
            for no, text in self._convert(pdf_bytes, first, last).items():
                pages.append(Page(no, text, "DOCLING", max(text_quality(text), HEAVY_THRESHOLD)))
        return pages
