"""Phase 2 (22 Nov 2026): reading pages. OCR reads a page Docling could not; a page whose text layer reads poorly is
read by OCR too in the standard pool; a document with many such pages goes to the heavy pool."""

from __future__ import annotations

import pytest

import dps.parse.engine as engine
from dps.parse.engine import HeavyEngine, Page, ParserEngine

GOOD = ("the Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times, tested on each "
        "Quarter Date, and shall deliver its audited accounts within 120 days. ") * 2
POOR = "x; " * 60  # sixty word characters among as many signs: a text layer that reads poorly (quality 0.4)


class _Ocr:
    def __init__(self, text: str = GOOD, confidence: float = 0.9) -> None:
        self.text, self.confidence, self.pages = text, confidence, []

    def ocr_page(self, pdf_bytes: bytes, page_index: int) -> tuple[str, float]:
        self.pages.append(page_index + 1)
        return self.text, self.confidence


class _Preset(ParserEngine):
    """The fast parse's pages are given; OCR and the fallback are the real code."""

    def __init__(self, pages: list[Page], ocr) -> None:
        super().__init__(ocr)
        self.preset = pages

    def parse(self, pdf_bytes: bytes) -> list[Page]:
        return list(self.preset)


def test_ocr_reads_the_pages_docling_could_not():
    """Bulk run: Docling failed on 44 runs of pages (not installed, models missing) and those pages stayed unread."""
    fast = [Page(1, GOOD, "TEXT", 0.95), Page(2, POOR, "TEXT", 0.4), Page(3, "", "NONE", 0.0),
            Page(4, "", "BLANK", 0.0)]
    ocr = _Ocr()

    def convert(pdf_bytes, first, last):
        raise ModuleNotFoundError("docling")

    pages = HeavyEngine(_Preset(fast, ocr), convert, chunk=10).parse(b"%PDF")
    assert [p.method for p in pages] == ["TEXT", "OCR", "OCR", "BLANK"]
    assert ocr.pages == [2, 3]  # only the pages Docling failed on; a good or blank page is not read again
    assert pages[0].text == GOOD


def test_a_worse_ocr_reading_does_not_replace_the_page():
    fast = [Page(1, POOR, "TEXT", 0.4)]
    pages = _Preset(fast, _Ocr(text="ab", confidence=0.95)).ocr_poor_pages(b"%PDF", fast, [1])
    assert pages[0].method == "TEXT" and pages[0].text == POOR


class _Reader:
    def __init__(self, texts: list[str]) -> None:
        self.pages = [_RawPage(t) for t in texts]


class _RawPage:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


def test_the_standard_pool_keeps_a_poor_text_layer_as_read(monkeypatch):
    """Measured on 72 real documents: OCR of the poor text pages (tables of contents, covers) in the standard pool
    changed one page and no finding, for minutes of OCR. Those pages keep their text; many of them send the document
    to the heavy pool (see the next test)."""
    texts = [GOOD] * 9 + [POOR]
    monkeypatch.setattr(engine, "PdfReader", lambda data: _Reader(texts))
    ocr = _Ocr()
    pages = ParserEngine(ocr).parse(b"%PDF")
    assert ocr.pages == [] and pages[9].method == "TEXT" and pages[9].text == POOR


def test_many_poor_pages_send_a_document_to_the_heavy_pool():
    from dps.ingest.pipeline import _poor_share

    def model(qualities):
        return {"pages": [{"no": i, "quality": q, "sourceMethod": "BLANK" if q is None else "TEXT"}
                          for i, q in enumerate(qualities, 1)]}

    assert _poor_share(model([0.9] * 7 + [0.3] * 3)) == pytest.approx(0.3)
    assert _poor_share(model([0.9] * 9 + [None] * 5)) == 0.0  # blank pages are not poor
    assert _poor_share({"pages": []}) == 0.0


# ---------- what the inspection lets through ----------
def _form_pdf(action_kind: str = "JavaScript") -> bytes:
    """A fillable form whose amount box runs a formatting script (First Abu Dhabi Bank's term-loan terms)."""
    import io

    import pikepdf
    from test_phase2_ingest import AGREEMENT, text_pdf

    pdf = pikepdf.open(io.BytesIO(text_pdf([AGREEMENT])))
    if action_kind == "JavaScript":
        action = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("AFNumber_Format(2,0,0,0,'',true);"))
    else:
        action = pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("cmd.exe"))
    field = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Tx, T=pikepdf.String("Amount"),
                                                 AA=pikepdf.Dictionary(F=pdf.make_indirect(action))))
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def test_a_form_fields_formatting_script_is_flagged_not_refused():
    from dps.ingest.inspect import InspectionFailed, inspect

    assert "FORM_SCRIPTS" in inspect(_form_pdf(), "PDF").flags
    with pytest.raises(InspectionFailed) as e:  # a form field that launches a program is still refused
        inspect(_form_pdf("Launch"), "PDF")
    assert e.value.code == "UNSAFE_CONTENT"


def test_the_heavy_pool_reads_longer_pdfs(monkeypatch):
    from test_phase2_ingest import text_pdf

    import dps.ingest.inspect as ins

    monkeypatch.setattr(ins, "MAX_PDF_PAGES", 2)
    three = text_pdf(["a", "b", "c"])
    with pytest.raises(ins.InspectionFailed) as e:
        ins.inspect(three, "PDF")
    assert e.value.code == "TOO_MANY_PAGES"
    assert ins.inspect(three, "PDF", max_pages=5).page_count == 3
    assert ins.MAX_PDF_PAGES_HEAVY == 1000


# ---------- a searchable scan is not read twice ----------
def test_a_searchable_scan_is_not_ocr_read_again_but_a_pasted_picture_still_is():
    """Bulk run: a 32-page trust deed and a 149-page annual report are pictures of their pages with the scanner's text
    layer behind them; every page was OCR-read again (352 s and 964 s) for lines the text layer already had."""
    from dps.parse.page_model import _read_picture_text

    ocr = _Ocr(text="Header\nThe Borrower shall maintain a DSCR of not less than 1.40x.")

    class Engine:
        _ocr = ocr

    model = {"flags": []}
    scan = Page(1, GOOD * 4, "TEXT", 0.96)  # 1,300 characters behind a full-page picture
    pasted = Page(2, "Header\nSchedule 4 (as scanned)", "TEXT", 0.9)  # a table pasted as a picture on part of the page
    out = _read_picture_text(b"", [scan, pasted], {1: {"imageShare": 0.98}, 2: {"imageShare": 0.7}}, Engine(), model)
    assert ocr.pages == [2]
    assert out[0].text == scan.text and "1.40x" in out[1].text
    assert model["flags"] == ["IMAGE_REGIONS", "SEARCHABLE_SCAN"]


def test_the_new_flags_follow_the_page_model_contract():
    """FORM_SCRIPTS and SEARCHABLE_SCAN are in page-model.json, and a reviewer is told what each means."""
    from test_phase2_ingest import AGREEMENT, sha, text_pdf, validate_page_model

    from dps.parse.page_model import build
    from dps.skills.runner import FILE_FLAGS

    data = text_pdf([AGREEMENT])
    model = build(data, "PDF", sha(data), "v1", ["FORM_SCRIPTS"], ParserEngine())
    model["flags"] = sorted(set(model["flags"]) | {"FORM_SCRIPTS", "SEARCHABLE_SCAN"})
    validate_page_model(model)
    assert {"FORM_SCRIPTS", "SEARCHABLE_SCAN"} <= set(FILE_FLAGS)


def test_the_sandbox_blocks_the_network_but_modules_still_import():
    """The sandbox replaced socket.socket with a function, so "import ssl" (class SSLSocket(socket)) raised TypeError
    and Docling never ran in the heavy pool (bulk run, 22 Nov 2026: 47 runs of pages failed)."""
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (f"import sys; sys.path.insert(0, {src!r})\n"
            "from dps.parse.sandbox import _no_network\n_no_network()\n"
            "import socket, ssl, urllib.request\n"
            "for f in (lambda: socket.socket(), lambda: socket.create_connection(('example.com', 443)),\n"
            "          lambda: urllib.request.urlopen('https://example.com', timeout=5)):\n"
            "    try:\n        f()\n        print('OPEN')\n"
            "    except Exception as ex:\n        print(type(ex).__name__)\n")
    # a fresh interpreter, so the network block does not reach the other tests; the code is fixed above
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)  # noqa: S603
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.split() == ["PermissionError", "PermissionError", "URLError"]


def test_the_ocr_reads_english_with_the_pp_ocrv6_text_model(monkeypatch, tmp_path):
    """RapidOCR's own model is for Chinese and ran English words together ("TheTrusteeishereby appointed"): on a
    Sri Lankan trust deed page it gave 152 words, 39 run together; PP-OCRv6 gave 450 and none (bulk run, 22 Nov
    2026). The OCR loads PP-OCRv6 where the image put it, and says so when it is missing."""
    import sys
    import types

    from dps.parse import engine as eng

    made = []
    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = lambda **kw: made.append(kw) or object()
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    model = tmp_path / "PP-OCRv6_rec_small.onnx"
    model.write_bytes(b"onnx")
    monkeypatch.setenv("DPS_OCR_REC_MODEL", str(model))
    eng.RapidOcrBackend()._ensure()
    assert made[-1] == {"rec_model_path": str(model)}
    monkeypatch.setenv("DPS_OCR_REC_MODEL", str(tmp_path / "missing.onnx"))
    monkeypatch.setenv("DPS_DOCLING_ARTIFACTS", str(tmp_path / "none"))
    monkeypatch.setattr(eng, "REC_MODEL_PLACES", (str(tmp_path / "none"),))
    eng.RapidOcrBackend()._ensure()
    assert made[-1] == {}  # RapidOCR's own model, with a warning in the log
