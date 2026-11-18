"""Inspection rules (Phase 2): every file passes these before any parser touches it.

A failure raises ``InspectionFailed`` with the catalogue code (TYPE_SPOOFED, CORRUPT, ENCRYPTED, UNSAFE_CONTENT,
TOO_MANY_PAGES, MACRO_PRESENT, TOO_LARGE); the job stops and the DMS is asked to quarantine the file. Passing
files come back with flags (REPAIRED, OWNER_RESTRICTED, HIDDEN_SHEETS ...) that travel into the page model.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_PDF_PAGES = 500
MAX_PAGE_SIDE_PT = 5000 / 25.4 * 72  # 5 m in PDF points
MAX_ZIP_ENTRIES = 10_000
MAX_UNPACKED = 200 * 1024 * 1024
MAX_ZIP_RATIO = 100
MAX_CSV_ROWS = 1_000_000
MAX_CSV_COLS = 500
FORMATS = ("PDF", "XLSX", "XLS", "CSV", "DOCX")
EXTENSIONS = {".pdf": "PDF", ".xlsx": "XLSX", ".xls": "XLS", ".csv": "CSV", ".docx": "DOCX"}
MACRO_EXTENSIONS = {".xlsm", ".xltm", ".docm", ".dotm", ".xlsb"}
ACTIVE_PDF_KEYS = ("/JavaScript", "/JS", "/Launch", "/EmbeddedFile", "/EmbeddedFiles", "/RichMedia", "/XFA")
_DDE = re.compile(rb"DDEAUTO|\bDDE\b", re.I)


class InspectionFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Inspection:
    format: str
    flags: list[str] = field(default_factory=list)
    page_count: int | None = None
    encoding: str | None = None


def declared_format(file_name: str | None, media_type: str | None = None) -> str:
    name = (file_name or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in MACRO_EXTENSIONS:
        raise InspectionFailed("MACRO_PRESENT", f"{ext} files can carry macros and are refused.")
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    mt = (media_type or "").lower()
    for key, fmt in (("pdf", "PDF"), ("spreadsheetml", "XLSX"), ("ms-excel", "XLS"), ("csv", "CSV"),
                     ("wordprocessingml", "DOCX")):
        if key in mt:
            return fmt
    raise InspectionFailed("UNSUPPORTED_FORMAT", "Only PDF, XLSX, XLS, CSV and DOCX files are read.")


def inspect(data: bytes, fmt: str) -> Inspection:
    """Runs every rule for the declared format; raises InspectionFailed on the first failure."""
    if fmt not in FORMATS:
        raise InspectionFailed("UNSUPPORTED_FORMAT", f"Unsupported format {fmt}.")
    _check_magic(data, fmt)
    return {"PDF": _inspect_pdf, "XLSX": _inspect_ooxml, "DOCX": _inspect_ooxml, "XLS": _inspect_xls,
            "CSV": _inspect_csv}[fmt](data, fmt)


def _check_magic(data: bytes, fmt: str) -> None:
    head = data[:1024]
    ok = {"PDF": PDF_MAGIC in head, "XLSX": data.startswith(ZIP_MAGIC), "DOCX": data.startswith(ZIP_MAGIC),
          "XLS": data.startswith(OLE_MAGIC), "CSV": _looks_text(data)}[fmt]
    if not ok:
        raise InspectionFailed("TYPE_SPOOFED", f"The file's content is not a {fmt} file.")


def _looks_text(data: bytes) -> bool:
    if data.startswith((PDF_MAGIC, ZIP_MAGIC, OLE_MAGIC)):
        return False
    return _decode(data[:65536]) is not None


def _decode(data: bytes) -> tuple[str, str] | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None
    if b"\x00" in data:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "cp1252" and sum(ch < " " and ch not in "\r\n\t" for ch in text) > len(text) * 0.01:
            return None
        return text, enc
    return None


# ---------- PDF ----------

def _inspect_pdf(data: bytes, fmt: str) -> Inspection:
    import pikepdf

    flags: list[str] = []
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError as ex:
        raise InspectionFailed("ENCRYPTED", "The PDF needs a password to open.") from ex
    except pikepdf.PdfError as ex:
        raise InspectionFailed("CORRUPT", "The PDF cannot be read.") from ex
    with pdf:
        if getattr(pdf, "is_repaired", False):  # pikepdf >= 9 reports qpdf's repair of a broken xref
            flags.append("REPAIRED")
        if pdf.is_encrypted:
            flags.append("OWNER_RESTRICTED")  # opened without a user password: owner-only restrictions
        try:
            pages = len(pdf.pages)
        except pikepdf.PdfError as ex:
            raise InspectionFailed("CORRUPT", "The PDF's page tree is broken.") from ex
        if pages == 0:
            raise InspectionFailed("CORRUPT", "The PDF has no pages.")
        if pages > MAX_PDF_PAGES:
            raise InspectionFailed("TOO_MANY_PAGES", f"The PDF has {pages} pages; the limit is {MAX_PDF_PAGES}.")
        for page in pdf.pages:
            box = [float(v) for v in page.mediabox]
            if max(abs(box[2] - box[0]), abs(box[3] - box[1])) > MAX_PAGE_SIDE_PT:
                raise InspectionFailed("CORRUPT", "A page is larger than 5 m on a side.")
        found = _active_content(pdf)
        if found:
            raise InspectionFailed("UNSAFE_CONTENT", f"The PDF holds active content ({found}).")
    return Inspection("PDF", flags, pages)


def _active_content(pdf) -> str | None:
    import pikepdf

    root = pdf.Root
    names = root.get("/Names")
    if names is not None:
        for key in ("/JavaScript", "/EmbeddedFiles"):
            if key in names:
                return key
    if "/AcroForm" in root and "/XFA" in root.AcroForm:
        return "/XFA"
    open_action = root.get("/OpenAction")
    if isinstance(open_action, pikepdf.Dictionary) and open_action.get("/S") in (pikepdf.Name.JavaScript,
                                                                                 pikepdf.Name.Launch):
        return "/OpenAction"
    for obj in pdf.objects:
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            continue
        keys = set(obj.keys())
        for key in ACTIVE_PDF_KEYS:
            if key in keys:
                return key
        if obj.get("/S") in (pikepdf.Name.JavaScript, pikepdf.Name.Launch):
            return str(obj.get("/S"))
        if obj.get("/Type") == pikepdf.Name.EmbeddedFile or obj.get("/Subtype") == pikepdf.Name.RichMedia:
            return "/EmbeddedFile"
    return None


# ---------- XLSX / DOCX ----------

def _inspect_ooxml(data: bytes, fmt: str) -> Inspection:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError) as ex:
        raise InspectionFailed("CORRUPT", "The file is not a readable Office file.") from ex
    if len(infos) > MAX_ZIP_ENTRIES:
        raise InspectionFailed("UNSAFE_CONTENT", "The file has too many parts.")
    unpacked = 0
    names = []
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or ":" in name:
            raise InspectionFailed("UNSAFE_CONTENT", "The file has an unsafe part name.")
        unpacked += info.file_size
        if unpacked > MAX_UNPACKED:
            raise InspectionFailed("UNSAFE_CONTENT", "The file unpacks to more than 200 MB.")
        if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
            raise InspectionFailed("UNSAFE_CONTENT", "The file is compressed suspiciously well (zip bomb).")
        names.append(name)
    lower = [n.lower() for n in names]
    main = "xl/workbook.xml" if fmt == "XLSX" else "word/document.xml"
    if main not in lower:
        raise InspectionFailed("TYPE_SPOOFED", f"The file is not a {fmt} file.")
    if any(n.endswith("vbaproject.bin") for n in lower):
        raise InspectionFailed("MACRO_PRESENT", "The file contains macros.")
    try:
        content_types = zf.read("[Content_Types].xml")
    except KeyError as ex:
        raise InspectionFailed("CORRUPT", "The file has no content types.") from ex
    if b"macroEnabled" in content_types:
        raise InspectionFailed("MACRO_PRESENT", "The file is macro-enabled.")
    if any(n.startswith(("xl/externallinks/", "word/embeddings/", "xl/embeddings/")) for n in lower):
        raise InspectionFailed("UNSAFE_CONTENT", "The file links to external workbooks or embeds objects.")
    flags: list[str] = []
    for name in names:
        low = name.lower()
        if not low.endswith((".xml", ".rels")):
            continue
        body = zf.read(name)
        if low.endswith(".rels") and b'TargetMode="External"' in body and b"hyperlink" not in body.lower():
            raise InspectionFailed("UNSAFE_CONTENT", "The file loads external content.")
        if (low.startswith("word/") or low.startswith("xl/")) and _DDE.search(body) and (
                b"instrText" in body or b"fldSimple" in body or b"ddeLink" in body or b"ddeService" in body):
            raise InspectionFailed("UNSAFE_CONTENT", "The file contains a DDE field.")
        if fmt == "DOCX" and low == "word/document.xml" and (b"<w:ins " in body or b"<w:del " in body):
            flags.append("TRACKED_CHANGES")
        if fmt == "XLSX" and low == "xl/workbook.xml" and (b'state="hidden"' in body
                                                           or b'state="veryHidden"' in body):
            flags.append("HIDDEN_SHEETS")
    return Inspection(fmt, flags)


# ---------- XLS ----------

def _inspect_xls(data: bytes, fmt: str) -> Inspection:
    try:
        from oletools.olevba import VBA_Parser
    except ImportError:  # pragma: no cover - oletools is in the worker extras
        VBA_Parser = None  # noqa: N806
    if VBA_Parser is not None:
        try:
            parser = VBA_Parser("file.xls", data=data)
            has_macros = parser.detect_vba_macros()
            parser.close()
        except Exception as ex:  # noqa: BLE001 - olevba raises many types on broken OLE files
            raise InspectionFailed("CORRUPT", "The XLS file cannot be read.") from ex
        if has_macros:
            raise InspectionFailed("MACRO_PRESENT", "The XLS file contains VBA macros.")
    import olefile

    try:
        ole = olefile.OleFileIO(data)
    except OSError as ex:
        raise InspectionFailed("CORRUPT", "The XLS file cannot be read.") from ex
    with ole:
        streams = {"/".join(s).lower() for s in ole.listdir()}
    if not ({"workbook", "book"} & streams):
        raise InspectionFailed("TYPE_SPOOFED", "The file is not an Excel 97-2003 workbook.")
    if any(s.startswith(("_vba_project", "macros")) for s in streams):
        raise InspectionFailed("MACRO_PRESENT", "The XLS file contains VBA macros.")
    if any(s.startswith(("mbd", "objectpool")) for s in streams):
        raise InspectionFailed("UNSAFE_CONTENT", "The XLS file embeds objects.")
    return Inspection("XLS", [])


# ---------- CSV ----------

def _inspect_csv(data: bytes, fmt: str) -> Inspection:
    decoded = _decode(data)
    if decoded is None:
        raise InspectionFailed("TYPE_SPOOFED", "The file is not a text CSV file.")
    text, encoding = decoded
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = 0
    flags: list[str] = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows += 1
        if rows > MAX_CSV_ROWS:
            raise InspectionFailed("TOO_LARGE", f"The CSV has more than {MAX_CSV_ROWS} rows.")
        if len(row) > MAX_CSV_COLS:
            raise InspectionFailed("TOO_LARGE", f"The CSV has more than {MAX_CSV_COLS} columns.")
        if "FORMULA_TEXT" not in flags and any(c[:1] in "=+-@" and not _is_number(c) for c in row if c):
            flags.append("FORMULA_TEXT")  # kept as text, never evaluated
    return Inspection("CSV", flags, encoding=encoding)


def _is_number(cell: str) -> bool:
    try:
        float(cell.replace(",", ""))
    except ValueError:
        return False
    return True
