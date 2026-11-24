"""Phase 2 (22 Nov 2026): reading pages. OCR reads a page Docling could not; a page whose text layer reads poorly is
read by OCR too in the standard pool; a document with many such pages goes to the heavy pool."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter

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


def test_long_heavy_document_slices_poor_pages_and_recovers_partial_docling_output():
    writer = PdfWriter()
    for _ in range(503):
        writer.add_blank_page(width=72, height=72)
    payload = io.BytesIO()
    writer.write(payload)
    fast = [Page(i, GOOD, "TEXT", 0.95) if i < 490 else Page(i, "", "NONE", 0.0) for i in range(1, 504)]
    requests = []
    ocr = _Ocr()

    def convert(data, first, last):
        requests.append((first, last))
        sliced = engine.pdf_slice(data, first, last)
        assert len(PdfReader(io.BytesIO(sliced)).pages) == last - first + 1
        return {i: GOOD for i in range(first, last + 1) if i != 503}

    pages = HeavyEngine(_Preset(fast, ocr), convert, chunk=10).parse(payload.getvalue())
    assert requests == [(490, 499), (500, 503)] and ocr.pages == [503]
    assert [p.no for p in pages] == list(range(1, 504))
    assert [p.method for p in pages[488:491]] == ["TEXT", "DOCLING", "DOCLING"]
    assert pages[-1].method == "OCR"


def test_docling_tables_preserve_statement_columns_and_covenant_levels():
    from dps.extraction.statement import rows_from_layout_table
    from dps.skills.covenants.rules import step_tables

    statement = "| Particulars | Note | 2024 | 2025 |\n| --- | --- | --- | --- |\n| Revenue | 4 | 3,500 | 5,000 |"
    assert rows_from_layout_table(7, statement, "STANDALONE") == [("Revenue", 5000.0, 7, "P7:L3:C4")]
    covenant = ("The Borrower shall not exceed the Debt to EBITDA ratio set out below:\n\n"
                "| Test date | Maximum leverage |\n| --- | --- |\n"
                "| 31 March 2027 | 4.00x |\n| 31 March 2028 | 3.50x |")
    [field] = step_tables([{"no": 8, "text": covenant}], [])
    assert field["source"]["page"] == 8 and field["status"] == "NEEDS_REVIEW"
    assert [row["threshold"] for row in field["value"]["schedule"]] == [4.0, 3.5]


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
    threads = {"intra_op_num_threads": 2, "inter_op_num_threads": 1}
    assert made[-1] == {**threads, "rec_model_path": str(model)}
    monkeypatch.setenv("DPS_OCR_REC_MODEL", str(tmp_path / "missing.onnx"))
    monkeypatch.setenv("DPS_DOCLING_ARTIFACTS", str(tmp_path / "none"))
    monkeypatch.setattr(eng, "REC_MODEL_PLACES", (str(tmp_path / "none"),))
    eng.RapidOcrBackend()._ensure()
    assert made[-1] == threads  # RapidOCR's own model, with a warning in the log


def test_one_european_looking_number_does_not_make_a_report_european():
    """A UAE group's statements ("Total assets 921,741", "AED' million") were read as European because of one
    "10,42" (two note numbers) on its pages: every amount 1,000 times too small (bulk run, 22 Nov 2026)."""
    from dps.extraction.statement import detect_number_style

    adq = "Notes 10,42 Total assets 921,741 Total liabilities 402,118 Equity 519,623"
    assert detect_number_style(adq) == "WESTERN"
    assert detect_number_style("Notes 10,42 and 7,11") is None  # weak signs alone tell nothing
    assert detect_number_style("Total 1.234.567,89 Umsatz 200.000,00 Kosten 45.120,50") == "EUROPEAN"
    assert detect_number_style("Revenue 1,234,567.89 Cost 45,120.50") == "WESTERN"


def test_a_unit_with_an_apostrophe_after_the_currency_is_read():
    """"AED' million" (and "AED’ million") is millions; it was read as crore, the default."""
    from dps.extraction.statement import _unit, unit_stated

    for head in ("Notes AED' million AED' million", "Notes AED’ million", "AED million"):
        assert _unit(head) == "million", head
        assert unit_stated(head)
    assert _unit("Rs.'000") == "thousand"


def test_a_shared_symbol_takes_the_currency_the_report_names():
    """A Sri Lankan report in "Rs.'000" and an Australian one in "$'000" were read as Indian rupees, the loan's
    default (bulk run, 22 Nov 2026). The bare symbol takes the currency the report names; None when it names none."""
    from dps.money import shared_symbol_currency as shared

    assert shared("Rs.'000", "Cargills (Ceylon) PLC amounts in Sri Lankan Rupees LKR") == "LKR"
    assert shared("$'000", "ASX Limited all amounts in Australian dollars (AUD)") == "AUD"
    assert shared("Rs. in crore", "figures in Indian Rupees INR") == "INR"
    assert shared("Rs. in crore", "no currency is named here") is None
    assert shared("AED' million", "Abu Dhabi AED") is None  # no shared symbol: the header names its own
    assert shared(None, None) is None


def test_dollar_m_over_the_columns_is_millions():
    """An Australian half-year report heads its columns "$m" (UK reports "£m"): it was read as crore with
    UNIT_NOT_STATED (bulk run, 23 Nov 2026). Only right after a money sign or code: "in m", "5m", "m2" are no unit."""
    from dps.extraction.statement import _unit, unit_stated

    for head in ("Note $m $m", "A$m", "US$m", "Notes £m", "€m", "AUD m"):
        assert _unit(head) == "million", head
        assert unit_stated(head), head
    for text in ("area in m", "a 5m loan", "floor 120 m2", "The term"):
        assert not unit_stated(text), text


def test_a_statement_takes_the_currency_its_whole_report_names(monkeypatch):
    """An annual report's statement pages head their columns "Rs.'000" or "$m" and name the currency only on other
    pages; the reader looked only at the statement pages and took the loan's default, INR (bulk run, 23 Nov 2026).
    The PDF reader keeps every page's text, and the currency rule reads it."""
    import io

    from dps.extraction import statement_pages as sp
    from dps.extraction import statement_pdf

    statement = "Statement of financial position As at 31 March Note Rs.'000 2024 Total assets 12 49,493,157"
    other = "All figures in this annual report are in Sri Lankan Rupees unless stated otherwise."
    monkeypatch.setattr(sp, "document_texts", lambda data, ocr=None: ([other, statement], True))
    monkeypatch.setattr(sp, "order_pages", lambda texts, short, entity="STANDALONE": ([1], 1))

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

        def extract_words(self, **kw):
            return []

    class Pdf:
        pages = [Page(other), Page(statement)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import pdfplumber

    monkeypatch.setattr(pdfplumber, "open", lambda f: Pdf())
    monkeypatch.setattr(statement_pdf, "_text_words", lambda page: [])
    got = statement_pdf.read_pdf_statement(io.BytesIO(b"%PDF").getvalue())
    assert "Sri Lankan Rupees" in got.document_text
    assert "Sri Lankan" not in got.header_text  # the statement page alone names no currency
    from dps.money import shared_symbol_currency

    assert shared_symbol_currency(got.header_text, got.document_text) == "LKR"


def test_note_printed_over_this_years_column_does_not_hide_it():
    """ASX's half-year report prints "Note" over its current column ("31 December 2024 Note $m"): the year's
    figures were taken for note numbers and last year's (30 June 2024) were read (bulk run, 23 Nov 2026)."""
    from dps.extraction.statement_pdf import _is_note_column, choose_column

    headers = {1: "31 December 2024 Note $m", 2: "30 June 2024 $m"}
    figures = {1: [546.4, 12265.9, 733.8, 16471.1], 2: [1243.1, 12176.9, 641.8, 16886.5]}
    assert not _is_note_column(headers[1], figures[1])
    assert choose_column(headers, figures) == 1
    assert _is_note_column("Note", [4.0, 5.0, 6.0])  # a real note column stays one
    assert _is_note_column("Note", [4.1, 4.2, 5.0])  # with sub-notes too
    assert _is_note_column("2024 Note", [12.0, 13.0, 14.0])  # dated heading, but note numbers below


def test_a_page_with_group_and_company_columns_is_not_weighed_as_the_group_only():
    """Sierra Cables heads its statements "Group | Company" on one page; each page was weighed as the group's only
    (-4), so a highlights page that names "the Company" (+4) was read: Total Assets 4.4 million rupees instead of
    13.08 billion (bulk run, 23 Nov 2026)."""
    from dps.extraction.statement_pages import _entity_weight

    both = "STATEMENT OF FINANCIAL POSITION Group Company As at 31st March Note 2025 2024 2025 2024 Rs. Rs."
    assert _entity_weight(both, "STANDALONE") == 0.0
    assert _entity_weight(both, "CONSOLIDATED") == 0.0
    assert _entity_weight("STATEMENT OF FINANCIAL POSITION - GROUP As at 31 March", "STANDALONE") == -4.0
    assert _entity_weight("COMPANY STATEMENT OF FINANCIAL POSITION As at 31 March", "STANDALONE") == 4.0


def test_revenues_is_revenue_and_revenue_in_advance_is_not():
    """ADQ's income statement says "Revenues 127,890": the plural was no revenue line, and a cash-flow adjustment
    "Revenue recognised from operating financial assets (1,584)" was read instead. ASX's balance sheet line "Revenue
    received in advance" (a liability) was read as the year's revenue (bulk run, 23 Nov 2026)."""
    from dps.extraction.statement import extract_values, match_key

    assert match_key("Revenues 7") == "REVENUE"
    for liability in ("Revenue received in advance B3", "Deferred revenue", "Unearned revenue", "Payments in advance"):
        assert match_key(liability) is None, liability
    rows = [("Revenues", 127890.0, 13, None), ("Revenue recognised from operating financial assets", -1584.0, 17, None)]
    got = {v.key: v for v in extract_values(rows, "AED million", currency="AED").values}
    assert got["REVENUE"].value == 127890.0 and got["REVENUE"].page == 13


def test_a_key_financial_ratios_page_is_no_statement():
    """ASX's annual report has a "Key financial ratios" page (five years of ratios, "EBIT/operating revenue 5, 6
    58.5%"): with no total revenue line in the income statement, its footnote mark 5 was read as revenue (bulk run,
    23 Nov 2026). Such a page is no statement; a revenue the statements do not print stays missing."""
    from dps.extraction.statement_pages import page_score

    ratios = ("Key financial ratios FY24 FY23 FY22 FY21 FY20 Basic earnings per share 244.8c 163.9c "
              "Total revenue 1,000.0 900.0 Total assets 16,886.5 16,342.7 Total liabilities 13,161.8")
    plain = ratios.replace("Key financial ratios", "Consolidated balance sheet")
    for kind in ("BS", "PL"):
        assert page_score(ratios, kind) <= page_score(plain, kind) - 6, kind
