"""Phase 2: storage, inspection (hostile corpus), page models for five formats, segmentation, sandbox, worker."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import jsonschema
import pikepdf
import pytest
from referencing import Registry, Resource

from dps.ingest.inspect import InspectionFailed, declared_format, inspect
from dps.ingest.store import LocalStore, StoreError
from dps.parse import sandbox
from dps.parse.engine import Page, ParserEngine
from dps.parse.page_model import build, to_pages
from dps.parse.segment import segment

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_page_model(model: dict) -> None:
    resources = []
    for path in (CONTRACTS / "schemas").glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    schema = json.loads((CONTRACTS / "schemas" / "page-model.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, registry=Registry().with_resources(resources)).validate(model)


# ---------- sample files ----------

def text_pdf(pages: list[str]) -> bytes:
    """A real PDF with a text layer, one string per page (Helvetica, one line per text line)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    for text in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for line in text.splitlines():
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        page = pikepdf.Page(pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 595, 842],
                                               Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))))
        page.obj.Contents = pdf.make_stream("\n".join(ops).encode("latin-1"))
        pdf.pages.append(page)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


AGREEMENT = ("FACILITY AGREEMENT\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.20 "
             "times,\ntested on each Quarter Date on the basis of the consolidated financial statements for the "
             "period.")


def with_root(data: bytes, **entries) -> bytes:
    pdf = pikepdf.open(io.BytesIO(data))
    for key, value in entries.items():
        pdf.Root[f"/{key}"] = value(pdf) if callable(value) else value
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def xlsx_bytes(hidden: bool = False) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "BS"
    ws["A1"], ws["B1"] = "Balance sheet as at 31 March 2026 (in ₹ lakh)", None
    ws.merge_cells("A1:C1")
    ws["A2"], ws["B2"] = "Revenue", 1200.5
    ws["A3"], ws["B3"] = "EBITDA", 300
    ws["B4"] = "=B2-B3"
    ws["B2"].number_format = "#,##0.00"
    if hidden:
        wb.create_sheet("Notes").sheet_state = "hidden"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def xls_bytes() -> bytes:
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("PL")
    ws.write(0, 0, "Revenue")
    ws.write(0, 1, 1500)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def docx_bytes(tracked: bool = False, dde: bool = False) -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("Compliance Certificate", level=1)
    d.add_paragraph("We certify that the DSCR for the quarter was 1.35 times.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Ratio", "Value"
    t.cell(1, 0).text, t.cell(1, 1).text = "DSCR", "1.35"
    out = io.BytesIO()
    d.save(out)
    data = out.getvalue()
    if not (tracked or dde):
        return data
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename == "word/document.xml":
                extra = b""
                if tracked:
                    extra += (b'<w:p><w:ins w:id="1" w:author="a" w:date="2026-01-01T00:00:00Z"><w:r><w:t>added'
                              b'</w:t></w:r></w:ins><w:del w:id="2" w:author="a" w:date="2026-01-01T00:00:00Z">'
                              b'<w:r><w:delText>gone</w:delText></w:r></w:del></w:p>')
                if dde:
                    extra += (b'<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DDEAUTO '
                              b'c:\\\\windows\\\\system32\\\\cmd.exe "/k calc"</w:instrText></w:r></w:p>')
                body = body.replace(b"<w:sectPr", extra + b"<w:sectPr", 1)
            dst.writestr(item, body)
    return buf.getvalue()


def zip_with(base: bytes, name: str, payload: bytes, compress=zipfile.ZIP_DEFLATED) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(base))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(zipfile.ZipInfo(name), payload, compress_type=compress)
    return buf.getvalue()


# ---------- storage ----------

@pytest.fixture
def store(tmp_path) -> LocalStore:
    for d in ("blobs", "staging", "derived"):
        (tmp_path / d).mkdir()
    return LocalStore(tmp_path / "blobs", tmp_path / "staging", tmp_path / "derived", max_bytes=1024 * 1024)


def test_store_checks_hash_while_reading(store):
    (store.blobs / "v1.pdf").write_bytes(b"hello")
    assert store.open("v1.pdf", sha(b"hello")) == b"hello"
    with pytest.raises(StoreError) as e:
        store.open("v1.pdf", sha(b"other"))
    assert e.value.code == "INTEGRITY_MISMATCH"
    (store.staging / "s1").write_bytes(b"x")
    assert store.open_staged("s1", sha(b"x")) == b"x"


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs", "C:\\x", "a/../../b", ""])
def test_store_refuses_keys_outside_its_folder(store, key):
    with pytest.raises(StoreError) as e:
        store.open(key, "0" * 64)
    assert e.value.code == "INVALID_INPUT"


def test_store_size_cap_and_missing(store):
    (store.blobs / "big").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(StoreError) as e:
        store.open("big", "0" * 64)
    assert e.value.code == "TOO_LARGE"
    with pytest.raises(StoreError) as e:
        store.open("none", "0" * 64)
    assert e.value.code == "NOT_FOUND"


def test_derived_is_write_once_and_rechecked(store):
    seen = []
    store.on_derived = seen.append
    path = LocalStore.derived_path("default", "v1", "pages", "1.png")
    rec = store.put_derived("default", "v1", path, b"png", "PAGE_IMAGE")
    assert seen == [rec] and rec.sha256 == sha(b"png") and store.has_derived(path)
    with pytest.raises(StoreError) as e:
        store.put_derived("default", "v1", path, b"other", "PAGE_IMAGE")
    assert e.value.code == "ARTIFACT_EXISTS"
    assert store.get_derived(path, rec.sha256) == b"png"
    with pytest.raises(StoreError):
        store.get_derived(path, sha(b"tampered"))
    with pytest.raises(StoreError):
        store.put_derived("default", "v1", "other/v2/x", b"x", "OCR")
    with pytest.raises(StoreError):
        LocalStore.derived_path("default", "..", "x")


# ---------- inspection: hostile corpus ----------

def js_pdf() -> bytes:
    return with_root(text_pdf([AGREEMENT]), OpenAction=lambda pdf: pikepdf.Dictionary(
        S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)")))


def launch_pdf() -> bytes:
    return with_root(text_pdf([AGREEMENT]), OpenAction=lambda pdf: pikepdf.Dictionary(
        S=pikepdf.Name.Launch, F=pikepdf.String("cmd.exe")))


def embedded_pdf() -> bytes:
    pdf = pikepdf.open(io.BytesIO(text_pdf([AGREEMENT])))
    spec = pikepdf.AttachedFileSpec(pdf, b"MZ\x90\x00 fake exe", filename="run.exe")
    pdf.attachments["run.exe"] = spec
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def encrypted_pdf() -> bytes:
    pdf = pikepdf.open(io.BytesIO(text_pdf([AGREEMENT])))
    out = io.BytesIO()
    pdf.save(out, encryption=pikepdf.Encryption(user="u-test", owner="o-test"))
    return out.getvalue()


def owner_only_pdf() -> bytes:
    pdf = pikepdf.open(io.BytesIO(text_pdf([AGREEMENT])))
    out = io.BytesIO()
    pdf.save(out, encryption=pikepdf.Encryption(user="", owner="o-test",
                                                allow=pikepdf.Permissions(extract=False)))
    return out.getvalue()


def zip_bomb() -> bytes:
    return zip_with(xlsx_bytes(), "xl/media/pad.bin", b"\x00" * (5 * 1024 * 1024))


HOSTILE = [
    ("PDF", js_pdf, "UNSAFE_CONTENT"),
    ("PDF", launch_pdf, "UNSAFE_CONTENT"),
    ("PDF", embedded_pdf, "UNSAFE_CONTENT"),
    ("PDF", encrypted_pdf, "ENCRYPTED"),
    ("PDF", lambda: text_pdf([AGREEMENT])[:300], "CORRUPT"),
    ("PDF", lambda: b"MZ\x90\x00 not a pdf", "TYPE_SPOOFED"),
    ("XLSX", zip_bomb, "UNSAFE_CONTENT"),
    ("XLSX", lambda: zip_with(xlsx_bytes(), "xl/vbaProject.bin", b"vba"), "MACRO_PRESENT"),
    ("DOCX", lambda: zip_with(docx_bytes(), "word/vbaProject.bin", b"vba"), "MACRO_PRESENT"),
    ("DOCX", lambda: docx_bytes(dde=True), "UNSAFE_CONTENT"),
    ("XLSX", lambda: zip_with(xlsx_bytes(), "xl/externalLinks/externalLink1.xml", b"<x/>"), "UNSAFE_CONTENT"),
    ("XLSX", lambda: zip_with(xlsx_bytes(), "../evil.txt", b"x"), "UNSAFE_CONTENT"),
    ("DOCX", xlsx_bytes, "TYPE_SPOOFED"),
    ("XLS", lambda: text_pdf([AGREEMENT]), "TYPE_SPOOFED"),
    ("CSV", lambda: b"\x00\x01\x02binary", "TYPE_SPOOFED"),
]


@pytest.mark.parametrize(("fmt", "make", "code"), HOSTILE, ids=[f"{f}-{c}-{i}" for i, (f, _, c) in enumerate(HOSTILE)])
def test_hostile_files_end_with_their_code(fmt, make, code):
    with pytest.raises(InspectionFailed) as e:
        inspect(make(), fmt)
    assert e.value.code == code


def test_macro_extensions_are_refused_by_type():
    for name in ("a.docm", "b.xlsm", "c.XLSB"):
        with pytest.raises(InspectionFailed) as e:
            declared_format(name)
        assert e.value.code == "MACRO_PRESENT"
    assert declared_format("x.PDF") == "PDF"
    assert declared_format("noext", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "XLSX"
    with pytest.raises(InspectionFailed):
        declared_format("a.exe")


def test_xls_with_vba_is_refused(monkeypatch):
    import oletools.olevba as olevba

    class FakeParser:
        def __init__(self, *_a, **_k): ...
        def detect_vba_macros(self): return True
        def close(self): ...

    monkeypatch.setattr(olevba, "VBA_Parser", FakeParser)
    with pytest.raises(InspectionFailed) as e:
        inspect(xls_bytes(), "XLS")
    assert e.value.code == "MACRO_PRESENT"


def test_csv_formula_cells_are_kept_as_text_and_flagged():
    data = b"Item,Amount\n=cmd|' /C calc'!A0,10\nRevenue,-5\n"
    report = inspect(data, "CSV")
    assert report.flags == ["FORMULA_TEXT"]
    model = build(data, "CSV", sha(data), flags=report.flags, encoding=report.encoding)
    assert model["sheets"][0]["cells"][2]["value"] == "=cmd|' /C calc'!A0"
    assert model["sheets"][0]["cells"][2]["formula"] is None


def test_csv_limits(monkeypatch):
    import dps.ingest.inspect as ins

    monkeypatch.setattr(ins, "MAX_CSV_ROWS", 3)
    with pytest.raises(InspectionFailed) as e:
        inspect(b"a\n1\n2\n3\n4\n", "CSV")
    assert e.value.code == "TOO_LARGE"
    wide = io.StringIO()
    csv.writer(wide).writerow(range(501))
    with pytest.raises(InspectionFailed) as e:
        inspect(wide.getvalue().encode(), "CSV")
    assert e.value.code == "TOO_LARGE"


def test_pdf_limits_and_flags(monkeypatch):
    import dps.ingest.inspect as ins

    assert inspect(owner_only_pdf(), "PDF").flags == ["OWNER_RESTRICTED"]
    monkeypatch.setattr(ins, "MAX_PDF_PAGES", 1)
    with pytest.raises(InspectionFailed) as e:
        inspect(text_pdf(["a", "b"]), "PDF")
    assert e.value.code == "TOO_MANY_PAGES"


def test_clean_files_pass_with_flags():
    assert inspect(text_pdf([AGREEMENT]), "PDF").page_count == 1
    assert inspect(xlsx_bytes(hidden=True), "XLSX").flags == ["HIDDEN_SHEETS"]
    assert inspect(docx_bytes(tracked=True), "DOCX").flags == ["TRACKED_CHANGES"]
    assert inspect(xls_bytes(), "XLS").format == "XLS"
    assert inspect("Item;Amount\nRevenue;10\n".encode("utf-16"), "CSV").encoding == "utf-16"


# ---------- page models ----------

def test_pdf_page_model_keeps_parser_text_and_adds_boxes():
    data = text_pdf([AGREEMENT, "SCHEDULE 1\nRepayment schedule for the facility in twelve quarterly instalments "
                                "starting June 2026."])
    model = build(data, "PDF", sha(data), "v1", engine=ParserEngine())
    validate_page_model(model)
    assert model["pageCount"] == 2 and model["pages"][0]["method"] == "NATIVE"
    assert any("bbox" in b for b in model["pages"][0]["blocks"])
    assert model["pages"][0]["width"] == 595
    assert [p.text for p in to_pages(model)] == [p.text for p in ParserEngine().parse(data)]
    assert [s["type"] for s in model["segments"]] == ["LOAN_AGREEMENT", "SCHEDULE"]


def test_xlsx_page_model_has_values_formulas_merges_and_hidden_sheets():
    data = xlsx_bytes(hidden=True)
    model = build(data, "XLSX", sha(data), flags=["HIDDEN_SHEETS"])
    validate_page_model(model)
    cells = {c["ref"]: c for c in model["sheets"][0]["cells"]}
    assert cells["B4"]["formula"] == "=B2-B3" and cells["B2"]["numberFormat"] == "#,##0.00"
    assert model["sheets"][0]["merged"] == ["A1:C1"] and model["sheets"][1]["hidden"] is True
    assert "₹ lakh" in model["pages"][0]["text"] and "HIDDEN_SHEETS" in model["flags"]


def test_xls_csv_docx_page_models():
    xls = xls_bytes()
    m = build(xls, "XLS", sha(xls))
    validate_page_model(m)
    assert m["sheets"][0]["cells"][1] == {"ref": "B1", "value": 1500.0, "formula": None, "numberFormat": None}
    data = b"Item,Amount\nRevenue,10\n"
    validate_page_model(build(data, "CSV", sha(data)))
    d = docx_bytes(tracked=True)
    m = build(d, "DOCX", sha(d), flags=["TRACKED_CHANGES"])
    validate_page_model(m)
    text = m["pages"][0]["text"]
    assert "Compliance Certificate" in text and "added" in text and "gone" not in text
    assert m["pages"][0]["blocks"][0]["kind"] == "HEADING" and m["pages"][0]["tables"][0]["rows"] == 2
    assert m["segments"][0]["type"] == "COMPLIANCE_CERTIFICATE"


def test_segmentation_rules():
    pages = [{"no": 1, "text": "TERM LOAN AGREEMENT\nbetween"}, {"no": 2, "text": "clauses"},
             {"no": 3, "text": "THE FIRST SCHEDULE\nRepayment"}, {"no": 4, "text": "Page 1 of 2\nnotes"},
             {"no": 5, "text": "COMPLIANCE CERTIFICATE\nWe certify"}]
    segs = segment({"format": "PDF", "pages": pages})
    assert [(s["fromPage"], s["toPage"], s["type"], s["uncertain"]) for s in segs] == [
        (1, 2, "LOAN_AGREEMENT", False), (3, 3, "SCHEDULE", True), (4, 4, None, True),
        (5, 5, "COMPLIANCE_CERTIFICATE", False)]
    assert segment({"format": "PDF", "pages": []}) == []


def test_to_pages_maps_methods():
    model = {"pages": [{"no": 1, "method": "LAYOUT", "quality": 0.9, "blocks": [{"text": "a"}, {"text": "b"}]}]}
    assert to_pages(model) == [Page(1, "a\nb", "DOCLING", 0.9)]


# ---------- sandbox ----------

def test_sandbox_returns_a_page_model_and_inspection_codes():
    data = text_pdf([AGREEMENT])
    model = sandbox.run(sandbox.ParseRequest(data, "PDF", sha(data), "v1", ocr=False), timeout=120)
    assert model["schema"] == "dps.page-model/1.0"
    with pytest.raises(InspectionFailed) as e:
        sandbox.run(sandbox.ParseRequest(js_pdf(), "PDF", "0" * 64, ocr=False), timeout=120)
    assert e.value.code == "UNSAFE_CONTENT"


def test_sandbox_survives_crash_and_hang_and_has_no_network():
    req = sandbox.ParseRequest(b"", "PDF", "0" * 64, ocr=False)
    with pytest.raises(sandbox.ParserCrash):
        sandbox.run(req, target="crash", timeout=120)
    with pytest.raises(sandbox.ParseTimeout):
        sandbox.run(req, target="hang", timeout=8)
    with pytest.raises(sandbox.ParserCrash) as e:
        sandbox.run(req, target="network", timeout=120)
    assert "PermissionError" in str(e.value)


def test_limits_scale_with_pages():
    lim = sandbox.Limits()
    assert lim.wall_clock(5) == 120 and lim.wall_clock(50) == 300 and lim.wall_clock(5000) == 900
    assert sandbox.Limits.for_pool("HEAVY").wall_clock(20) == 360
    assert sandbox.Limits.for_pool("HEAVY").memory_bytes == 6 * sandbox.GB


@pytest.mark.skipif(not hasattr(__import__("socket"), "AF_UNIX") or sys.platform == "win32",
                    reason="Unix sockets: Linux parser container only")
def test_socket_parser_round_trip(tmp_path):
    import threading

    from dps.parse import server

    path = str(tmp_path / "p.sock")
    srv = server.socketserver.ThreadingUnixStreamServer(path, server._Handler)
    srv.pool = "STANDARD"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        data = text_pdf([AGREEMENT])
        client = server.SocketParser(path)
        assert client.parse(sandbox.ParseRequest(data, "PDF", sha(data), ocr=False))["pageCount"] == 1
        with pytest.raises(InspectionFailed):
            client.parse(sandbox.ParseRequest(js_pdf(), "PDF", "0" * 64, ocr=False))
    finally:
        srv.shutdown()


# ---------- fuzzing (small in CI; the full 10,000 run is scripts/fuzz_parser.py) ----------

def test_mutated_files_never_escape_inspection_or_parsing():
    import random

    rnd = random.Random(7)  # noqa: S311 - deterministic fuzzing, not security
    seeds = [("PDF", text_pdf([AGREEMENT])), ("XLSX", xlsx_bytes())]
    for fmt, seed in seeds:
        for _ in range(40):
            data = bytearray(seed)
            for _ in range(rnd.randint(1, 20)):
                data[rnd.randrange(len(data))] = rnd.randrange(256)
            try:
                report = inspect(bytes(data), fmt)
                build(bytes(data), fmt, sha(bytes(data)), flags=report.flags, engine=ParserEngine())
            except InspectionFailed:
                pass
            except Exception as ex:  # noqa: BLE001 - parsers may fail; in production that is the sandbox's job
                assert not isinstance(ex, (SystemExit, KeyboardInterrupt))


def test_gzip_page_model_round_trip():
    data = text_pdf([AGREEMENT])
    model = build(data, "PDF", sha(data), engine=ParserEngine())
    raw = gzip.compress(json.dumps(model).encode(), mtime=0)
    assert json.loads(gzip.decompress(raw)) == model
