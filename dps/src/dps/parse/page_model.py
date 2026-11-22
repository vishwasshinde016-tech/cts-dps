"""Page model builders (Phase 2): every format becomes one ``dps.page-model/1.0`` document.

Coordinates are PDF points, origin top-left, boxes ``[x0, y0, x1, y1]``. For PDFs the page ``text`` is exactly
what ``ParserEngine`` produced (native text, OCR for pages without a text layer), so the existing flows read the
page model with unchanged results; pdfplumber adds line boxes and tables for citations and later stages.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime

from dps.parse.engine import Page, ParserEngine

log = logging.getLogger(__name__)

SCHEMA = "dps.page-model/1.0"
METHOD = {"TEXT": "NATIVE", "OCR": "OCR", "DOCLING": "LAYOUT", "NONE": "NATIVE", "BLANK": "NATIVE"}
MAX_SHEET_CELLS = 500_000


def _base(fmt: str, sha256: str, version_id: str | None, flags: list[str]) -> dict:
    return {"schema": SCHEMA, "documentVersionId": version_id, "sha256": sha256, "format": fmt, "pageCount": 0,
            "language": None, "flags": sorted(set(flags)), "pages": [], "sheets": [], "segments": []}


def build(data: bytes, fmt: str, sha256: str, version_id: str | None = None, flags: list[str] | None = None,
          engine: ParserEngine | None = None, encoding: str | None = None) -> dict:
    flags = list(flags or [])
    if fmt == "PDF":
        model = build_pdf(data, sha256, version_id, flags, engine or ParserEngine())
    elif fmt == "XLSX":
        model = build_xlsx(data, sha256, version_id, flags)
    elif fmt == "XLS":
        model = build_xls(data, sha256, version_id, flags)
    elif fmt == "CSV":
        model = build_csv(data, sha256, version_id, flags, encoding)
    elif fmt == "DOCX":
        model = build_docx(data, sha256, version_id, flags)
    else:
        raise ValueError(f"unsupported format {fmt}")
    from dps.parse.segment import segment

    model["segments"] = segment(model)
    return model


# ---------- PDF ----------

def build_pdf(data: bytes, sha256: str, version_id: str | None, flags: list[str], engine: ParserEngine) -> dict:
    model = _base("PDF", sha256, version_id, flags)
    pages = engine.parse(data)
    geometry = _pdf_geometry(data)
    pages = _read_picture_text(data, pages, geometry, engine, model)
    for page in pages:
        geo = geometry.get(page.no, {})
        entry = {"no": page.no, "method": METHOD.get(page.method, "NATIVE"), "sourceMethod": page.method,
                 "quality": max(0.0, min(1.0, float(page.quality))), "text": page.text,
                 "blocks": _blocks(page, geo.get("lines", [])), "tables": geo.get("tables", [])}
        if "width" in geo:
            entry["width"], entry["height"] = geo["width"], geo["height"]
        model["pages"].append(entry)
    model["pageCount"] = len(pages)
    if pages and sum(p.quality < 0.5 and p.method != "BLANK" for p in pages) > 0.25 * len(pages):
        model["flags"] = sorted(set(model["flags"]) | {"POOR_TEXT"})
    return model


IMAGE_SHARE = 0.2  # a text page whose pictures cover more than this is also OCR-read
# A searchable scan: the whole page is one picture and its text layer is that picture's OCR, already good. Reading the
# picture again only repeated the same lines and took most of the time (bulk run, 22 Nov 2026: a 149-page annual
# report 964 s, a 32-page trust deed 352 s). A picture pasted on part of a text page (a table) is still read.
SCAN_SHARE, SCAN_TEXT_CHARS, SCAN_TEXT_QUALITY = 0.9, 800, 0.8


def _read_picture_text(data: bytes, pages: list[Page], geometry: dict, engine: ParserEngine, model: dict) -> list[Page]:
    """Full review F9: a page with a text layer was never OCR-read, so a table or clause pasted on it as a picture was
    lost. The picture share comes from the pdfplumber pass that runs anyway; such pages are OCR-read too and the OCR
    lines not already in the text are added. Flagged IMAGE_REGIONS. Without OCR the page is flagged only."""
    out = []
    for page in pages:
        share = (geometry.get(page.no) or {}).get("imageShare", 0.0)
        if page.method != "TEXT" or share <= IMAGE_SHARE:
            out.append(page)
            continue
        if share >= SCAN_SHARE and len(page.text) >= SCAN_TEXT_CHARS and page.quality >= SCAN_TEXT_QUALITY:
            model["flags"] = sorted(set(model["flags"]) | {"SEARCHABLE_SCAN"})
            out.append(page)
            continue
        model["flags"] = sorted(set(model["flags"]) | {"IMAGE_REGIONS"})
        ocr = getattr(engine, "_ocr", None)
        if ocr is None:
            out.append(page)
            continue
        try:
            text, _ = ocr.ocr_page(data, page.no - 1)
        except Exception:  # noqa: BLE001 - the native text stands; the flag tells the reviewer
            out.append(page)
            continue
        have = " ".join(page.text.split()).lower()
        extra = [ln.strip() for ln in text.splitlines() if ln.strip() and " ".join(ln.split()).lower() not in have]
        out.append(Page(page.no, page.text + ("\n" + "\n".join(extra) if extra else ""), page.method, page.quality))
    return out


def _pdf_geometry(data: bytes) -> dict[int, dict]:
    """Line boxes and tables per page from pdfplumber; empty when it is not installed or fails."""
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - pdfplumber is in the worker extras
        return {}
    out: dict[int, dict] = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                lines = []
                for line in page.extract_text_lines(return_chars=False) or []:
                    lines.append((line["text"], [round(line["x0"], 1), round(line["top"], 1),
                                                 round(line["x1"], 1), round(line["bottom"], 1)]))
                tables = []
                for t_no, table in enumerate(page.find_tables(), start=1):
                    rows = table.extract() or []
                    cells = [{"r": r, "c": c, "text": (v or "").strip()}
                             for r, row in enumerate(rows) for c, v in enumerate(row) if v is not None]
                    tables.append({"id": f"p{i}t{t_no}", "bbox": [round(v, 1) for v in table.bbox],
                                   "rows": len(rows), "cols": max((len(r) for r in rows), default=0),
                                   "cells": cells})
                area = float(page.width * page.height) or 1.0
                pictures = sum(max(0.0, im["x1"] - im["x0"]) * max(0.0, im["bottom"] - im["top"])
                               for im in page.images)
                out[i] = {"width": round(float(page.width), 1), "height": round(float(page.height), 1),
                          "lines": lines, "tables": tables, "imageShare": round(min(1.0, pictures / area), 3)}
                close = getattr(page, "close", None)  # free the page's layout cache: long PDFs stay small
                if callable(close):
                    close()
    except Exception as ex:  # noqa: BLE001 - positions are an extra; the text is already read
        log.warning("pdfplumber geometry failed: %s", type(ex).__name__)
        return out
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _blocks(page: Page, lines: list[tuple[str, list[float]]]) -> list[dict]:
    """One block per non-empty text line; a box is attached when pdfplumber saw the same line."""
    boxes: dict[str, list[list[float]]] = {}
    for text, box in lines:
        boxes.setdefault(_norm(text), []).append(box)
    blocks = []
    for n, line in enumerate(t for t in page.text.splitlines() if t.strip()):
        block = {"id": f"p{page.no}b{n + 1}", "kind": "PARAGRAPH", "text": line}
        found = boxes.get(_norm(line))
        if found:
            block["bbox"] = found.pop(0)
        if page.method == "OCR":
            block["ocrConfidence"] = page.quality
        blocks.append(block)
    return blocks


# ---------- spreadsheets ----------

def _cell_value(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _sheet_page(no: int, name: str, rows: list[list[str]]) -> dict:
    lines = [" | ".join(c for c in row if c) for row in rows]
    lines = [line for line in lines if line]
    return {"no": no, "method": "NATIVE", "quality": 1.0, "sheet": name, "text": "\n".join(lines),
            "blocks": [{"id": f"s{no}r{i + 1}", "kind": "CELL", "text": line} for i, line in enumerate(lines)]}


def build_xlsx(data: bytes, sha256: str, version_id: str | None, flags: list[str]) -> dict:
    from openpyxl import load_workbook

    model = _base("XLSX", sha256, version_id, flags)
    values = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    formulas = load_workbook(io.BytesIO(data), data_only=False, read_only=False)
    total = 0
    for no, ws in enumerate(values.worksheets, start=1):
        fws = formulas[ws.title]
        cells, rows = [], []
        for row in ws.iter_rows():
            texts = []
            for cell in row:
                formula = fws[cell.coordinate].value
                formula = formula if isinstance(formula, str) and formula.startswith("=") else None
                if cell.value is None and formula is None:
                    texts.append("")
                    continue
                total += 1
                if total > MAX_SHEET_CELLS:
                    raise ValueError("TOO_LARGE")
                if formula is not None and cell.value is None:  # F13: saved without values: the figure is missing
                    model["flags"] = sorted(set(model["flags"]) | {"NO_CACHED_VALUE"})
                fmt = cell.number_format if cell.number_format != "General" else None
                cells.append({"ref": cell.coordinate, "value": _cell_value(cell.value), "formula": formula,
                              "numberFormat": fmt})
                texts.append("" if cell.value is None else str(_cell_value(cell.value)))
            rows.append(texts)
        hidden = ws.sheet_state != "visible"
        if any(d.hidden for d in ws.row_dimensions.values()) or any(d.hidden for d in ws.column_dimensions.values()):
            model["flags"] = sorted(set(model["flags"]) | {"HIDDEN_ROWS_OR_COLUMNS"})  # F13: read, and said so
        model["sheets"].append({"name": ws.title, "hidden": hidden, "merged": [str(r) for r in ws.merged_cells.ranges],
                                "cells": cells})
        model["pages"].append(_sheet_page(no, ws.title, rows))
    if any(s["hidden"] for s in model["sheets"]):
        model["flags"] = sorted(set(model["flags"]) | {"HIDDEN_SHEETS"})
    model["pageCount"] = len(model["pages"])
    return model


def build_xls(data: bytes, sha256: str, version_id: str | None, flags: list[str]) -> dict:
    import xlrd
    from xlrd.xldate import xldate_as_datetime

    model = _base("XLS", sha256, version_id, flags)
    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    for no, name in enumerate(book.sheet_names(), start=1):
        sh = book.sheet_by_name(name)
        cells, rows = [], []
        for r in range(sh.nrows):
            texts = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    texts.append("")
                    continue
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    value = xldate_as_datetime(value, book.datemode).isoformat()
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = bool(value)
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    value = None
                cells.append({"ref": f"{xlrd.colname(c)}{r + 1}", "value": value, "formula": None,
                              "numberFormat": None})
                texts.append("" if value is None else str(value))
            rows.append(texts)
        hidden = bool(getattr(sh, "visibility", 0))
        merged = [f"{xlrd.colname(c0)}{r0 + 1}:{xlrd.colname(c1 - 1)}{r1}" for r0, r1, c0, c1 in sh.merged_cells]
        model["sheets"].append({"name": name, "hidden": hidden, "merged": merged, "cells": cells})
        model["pages"].append(_sheet_page(no, name, rows))
    book.release_resources()
    if any(s["hidden"] for s in model["sheets"]):
        model["flags"] = sorted(set(model["flags"]) | {"HIDDEN_SHEETS"})
    model["pageCount"] = len(model["pages"])
    return model


def build_csv(data: bytes, sha256: str, version_id: str | None, flags: list[str], encoding: str | None) -> dict:
    model = _base("CSV", sha256, version_id, flags)
    text = data.decode(encoding or "utf-8-sig", errors="replace")
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    cells, rows = [], []
    from openpyxl.utils import get_column_letter

    for r, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        rows.append(row)
        for c, value in enumerate(row, start=1):
            if value != "":
                cells.append({"ref": f"{get_column_letter(c)}{r}", "value": value, "formula": None,
                              "numberFormat": None})  # "=..." stays text, never evaluated
    model["sheets"].append({"name": "Sheet1", "hidden": False, "merged": [], "cells": cells})
    model["pages"].append(_sheet_page(1, "Sheet1", rows))
    model["pageCount"] = 1
    return model


# ---------- DOCX ----------

DOCX_PAGE_CHARS = 4_000  # a logical page: a heading starts one, and one never grows much past this


def build_docx(data: bytes, sha256: str, version_id: str | None, flags: list[str]) -> dict:
    """A Word file as logical pages (full review F6).

    Before, the whole file was one page: a long agreement never fit the agent's prompt and the page tool saw only its
    first 12,000 characters. Now a heading starts a new page and a page closes at about 4,000 characters, each block
    keeps its locator (``docx:para:N``, ``docx:tbl:T``, ``docx:footnote:N``). Read too: content controls (form fields,
    which were dropped), footnotes and endnotes. Tracked changes are read accepted, the deleted words kept on the block
    for the reviewer; TRACKED_CHANGES, COMMENTS and CONTENT_CONTROLS are flagged.
    """
    import zipfile

    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    model = _base("DOCX", sha256, version_id, flags)
    document = docx.Document(io.BytesIO(data))
    found: set[str] = set()
    blocks: list[dict] = []  # {"kind", "text", "ref", optional "deleted", "cells"}

    def add(kind: str, text: str, ref: str, **extra) -> None:
        if text.strip() or extra.get("deleted"):  # a fully deleted paragraph is kept for its deleted words
            blocks.append({"kind": kind, "text": text, "ref": ref, **extra})

    def walk(container, counter: list[int]) -> None:
        for child in container.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                counter[0] += 1
                p = Paragraph(child, document)
                style = (p.style.name if p.style is not None else "") or ""
                deleted = _deleted_text(child)
                if deleted:
                    found.add("TRACKED_CHANGES")
                if child.find(f".//{_W}ins") is not None:
                    found.add("TRACKED_CHANGES")
                add("HEADING" if style.lower().startswith(("heading", "title")) else "PARAGRAPH",
                    _accepted_text(child), f"docx:para:{counter[0]}", **({"deleted": deleted} if deleted else {}))
            elif tag == "tbl":
                counter[1] += 1
                table = Table(child, document)
                cells, rows = [], []
                for r, row in enumerate(table.rows):
                    texts = []
                    for c, cell in enumerate(row.cells):
                        cells.append({"r": r, "c": c, "text": cell.text.strip()})
                        texts.append(cell.text.strip())
                    rows.append(" | ".join(t for t in texts if t))
                add("TABLE", "\n".join(r for r in rows if r), f"docx:tbl:{counter[1]}", cells=cells,
                    shape=(len(table.rows), len(table.columns)))
            elif tag == "sdt":  # a content control: its paragraphs and tables are part of the text
                found.add("CONTENT_CONTROLS")
                content = child.find(f"{_W}sdtContent")
                if content is not None:
                    walk(content, counter)

    for section in document.sections:
        for p in section.header.paragraphs:
            add("PARAGRAPH", p.text, "docx:header")
    walk(document.element.body, [0, 0])
    for section in document.sections:
        for p in section.footer.paragraphs:
            add("PARAGRAPH", p.text, "docx:footer")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        for part, kind in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
            if part in names:
                for n, note in enumerate(_notes(z.read(part), kind), start=1):
                    add("FOOTNOTE", note, f"docx:{kind}:{n}")
        if "word/comments.xml" in names:
            found.add("COMMENTS")
    if document.inline_shapes and len(document.inline_shapes):
        found.add("IMAGES")

    pages: list[list[dict]] = [[]]
    size = 0
    for b in blocks:
        if pages[-1] and (b["kind"] == "HEADING" or size + len(b["text"]) > DOCX_PAGE_CHARS):
            pages.append([])
            size = 0
        pages[-1].append(b)
        size += len(b["text"]) + 1
    for no, page in enumerate((p for p in pages if p), start=1):
        out_blocks, tables = [], []
        for k, b in enumerate(page, start=1):
            block = {"id": f"p{no}b{k}", "kind": b["kind"], "text": b["text"], "ref": b["ref"]}
            if b.get("deleted"):
                block["deleted"] = b["deleted"]
            out_blocks.append(block)
            if b["kind"] == "TABLE":
                tables.append({"id": f"p{no}t{len(tables) + 1}", "rows": b["shape"][0], "cols": b["shape"][1],
                               "cells": b["cells"], "ref": b["ref"]})
        model["pages"].append({"no": no, "method": "NATIVE", "quality": 1.0,
                               "text": "\n".join(b["text"] for b in page), "blocks": out_blocks, "tables": tables})
    if not model["pages"]:
        model["pages"].append({"no": 1, "method": "NATIVE", "quality": 1.0, "text": "", "blocks": [], "tables": []})
    model["pageCount"] = len(model["pages"])
    model["flags"] = sorted(set(model["flags"]) | found)
    return model


def _deleted_text(p_element) -> str:
    """The words a tracked change deleted from this paragraph (kept for the reviewer, never read as text)."""
    return "".join(node.text or "" for node in p_element.iter() if node.tag == f"{_W}delText")


def _notes(xml: bytes, kind: str) -> list[str]:
    """The text of each footnote or endnote (the separator notes Word adds itself are skipped)."""
    from lxml import etree

    root = etree.fromstring(xml)  # noqa: S320 - the file passed the OOXML inspection (no DTD, no external parts)
    out = []
    for note in root.findall(f"{_W}{kind}"):
        if note.get(f"{_W}type") in ("separator", "continuationSeparator", "continuationNotice"):
            continue
        text = " ".join("".join(n.text or "" for n in p.iter(f"{_W}t")) for p in note.iter(f"{_W}p")).strip()
        if text:
            out.append(text)
    return out

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _accepted_text(p_element) -> str:
    """Paragraph text with tracked changes accepted: insertions kept, deletions dropped."""
    out = []
    for node in p_element.iter():
        if node.tag == f"{_W}t":
            parent_del = any(a.tag == f"{_W}del" for a in node.iterancestors())
            if not parent_del:
                out.append(node.text or "")
        elif node.tag == f"{_W}tab":
            out.append("\t")
    return "".join(out)


# ---------- back to the flows ----------

def to_pages(model: dict) -> list[Page]:
    """The flows' ``Page`` list from a page model: same text, method and quality as ParserEngine gave."""
    out = []
    for p in model.get("pages", []):
        method = p.get("sourceMethod") or {"NATIVE": "TEXT", "LAYOUT": "DOCLING"}.get(p["method"], p["method"])
        out.append(Page(p["no"], p.get("text") or "\n".join(b["text"] for b in p.get("blocks", [])), method,
                        p["quality"]))
    return out
