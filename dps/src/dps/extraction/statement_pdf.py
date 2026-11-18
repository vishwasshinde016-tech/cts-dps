"""Flow B Parse step for statement PDFs: read the table, not just the text (03-flows-design.md §8).

The plain-text reader takes the first number on each line. In real statements that is often the note
number ("Total equity  12  2,000  1,850"), a quarter, the previous year or a date, and it was right on
48% of the values in the statement-PDF benchmark (outputs/benchmarks/statement-pdf-readers.md). This
reader keeps each word's position instead:

1. Words with their positions: pdfplumber on pages with a text layer, RapidOCR boxes on scanned pages
   (the page is straightened first, using the slope of the boxes).
2. Lines of words, split into label + amounts. Words straight after an amount mean a sentence, not a
   table row, so narrative such as a directors' report is skipped; a wide gap starts a second table on
   the same line (statements printed side by side).
3. Amounts are grouped into columns by their right edge, and the header words above each column are
   kept. The column used is the current annual, standalone one: a note column, quarter or half-year
   columns, consolidated and previous-year columns are passed over, and the latest year wins.
4. Labels that wrap onto a second line are joined, and a bare "Borrowings" takes its meaning from the
   "Non-current liabilities" / "Current liabilities" heading above it.

The rows then go through the same extract_values() as every other format.
"""

from __future__ import annotations

import io
import logging
import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field

from dps.extraction.statement import LINE_ALIASES, european_to_comma, match_key, parse_number

log = logging.getLogger(__name__)

Row = tuple[str, float, int | None, str | None]

_AMOUNT = re.compile(r"^\(?-?(?:₹|\$)?[0-9][0-9,]*(?:\.[0-9]+)?\)?$")
_DASHES = {"-", "–", "—", "nil"}
# Currency marks and units printed next to amounts ("$ 32,494", "1,234.5 Cr", "LKR 1,000").
_UNITS = {"cr", "crore", "crores", "lakh", "lakhs", "rs", "₹", "inr", "mn", "million", "$", "us$", "lkr", "€", "£"}
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?:\s*-\s*(\d\d))?(?!\d)")
_SHORT_DATE = re.compile(r"(?<!\d)\d{1,2}[-/ ](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                         r"[-/ ](\d\d)(?!\d)", re.I)
_CHANGES = re.compile(r"changes?\s+in\s+(operating\s+)?(assets|working\s+capital)"
                      r"|working\s+capital\s+(changes|adjustments)", re.I)
_BARE_BORROWINGS = re.compile(r"(\(?[ivx]+\)\s*)?(interest[- ]bearing\s+)?(loans\s+(and|&)\s+)?borrowings", re.I)
_SECTIONS = [(re.compile(r"non[- ]current liabilities", re.I), "Long-term borrowings"),
             (re.compile(r"^(\(?[0-9ivxa-z]{1,4}[.)]\s*)?current liabilities", re.I), "Short-term borrowings")]
_BLOCK = re.compile(r"^(\(?[0-9ivxa-z]{1,4}[.)]\s*)?(non[- ]current|current)\s+(assets|liabilities)\s*:?$", re.I)
_GARBLE = re.compile(r"\(cid:\d+\)|[ĨǆƉǀďƋͿ]")
# A unit stated as a footnote under the table ("Note : All values are in Rs '000s, unless otherwise stated."):
# Sri Lankan interim statements put it at the foot of the page, past the heading text read for the unit.
_UNIT_NOTE = re.compile(r"\b(?:all\s+)?(?:values|amounts|figures)\s+(?:are\s+)?(?:stated\s+|shown\s+|expressed\s+)?"
                        r"in\b[^\n]{0,40}", re.I)
# Text pages need this many word characters; fewer means a scan (same rule as ParserEngine).
MIN_TEXT_CHARS = 40
CHAR_W = 5.0  # points; about one character at statement font sizes


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    height: float = 0.0


@dataclass
class Periods:
    """What period each page's figures cover, and the comparative columns beside them (12-month covenants)."""
    months: dict[int, int | None] = field(default_factory=dict)  # page -> 3, 6, 9, 12 or None (not stated)
    prior: list[Row] = field(default_factory=list)  # last year's same period
    annual: list[Row] = field(default_factory=list)  # the last full year, when an interim prints it
    annual_end: tuple[int, int] | None = None  # (year, month) the full-year column ends


@dataclass
class PdfStatement:
    rows: list[Row]
    header_text: str
    ocr_pages: list[int]
    ocr_quality: float | None  # mean OCR confidence over scanned pages, None when none were scanned
    texts: list[str] = field(default_factory=list)  # full text of each page read, for the second reader
    pages: list[int] = field(default_factory=list)  # the 1-based page number of each text
    periods: Periods = field(default_factory=Periods)


# ----------------------------------------------------------------------------------------------------
# Words
# ----------------------------------------------------------------------------------------------------
def _text_words(page) -> list[Word]:
    words: list[Word] = []
    for w in page.extract_words(x_tolerance=1.5):
        prev = words[-1] if words else None
        if (prev is not None and abs(prev.top - w["top"]) < 1 and abs(w["x0"] - prev.x1) < 1
                and (prev.text[-1:].isdigit() or (prev.text[-1:] == "," and prev.text[:-1].replace(",", "").isdigit()))
                and w["text"][:1] in "0123456789,"):
            # Some PDFs space the characters of one amount apart: "4" "1,651.24" is 41,651.24, and Sri Lanka run
            # (Sunshine): "5" "5" "3" "," "0" "8" "3" stopped at the comma and was read as 553.
            prev.text += w["text"]
            prev.x1 = w["x1"]
            continue
        if (prev is not None and abs(prev.top - w["top"]) < 1 and 0 <= w["x0"] - prev.x1 < 2 * CHAR_W
                and _COMMA_END.match(prev.text) and _THOUSANDS.match(w["text"])):
            # Sri Lanka run (Sunshine): "553, 083" set with a space after the comma was read as 553.
            prev.text += w["text"]
            prev.x1 = w["x1"]
            continue
        words.append(Word(w["text"], w["x0"], w["x1"], w["top"]))
    return words


_COMMA_END = re.compile(r"^\(?\d{1,3}(?:,\d{3})*,$")
_THOUSANDS = re.compile(r"^\d{3}(?:,\d{3})*(?:\.\d+)?\)?$")


def ocr_words(boxes: Iterable[tuple[list, str, float]], dpi: int) -> list[Word]:
    """RapidOCR boxes (four corner points in pixels, text, confidence) -> words in points, deskewed."""
    boxes = list(boxes)
    slopes = [math.atan2(b[1][1] - b[0][1], b[1][0] - b[0][0]) for b, _, _ in boxes if b[1][0] - b[0][0] > 60]
    angle = -statistics.median(slopes) if slopes else 0.0
    cos, sin = math.cos(angle), math.sin(angle)
    k = 72 / dpi
    words: list[Word] = []
    for box, text, _ in boxes:
        xs = [(x * cos - y * sin) * k for x, y in box]
        ys = [(x * sin + y * cos) * k for x, y in box]
        x0, x1, top, bottom = min(xs), max(xs), min(ys), max(ys)
        parts = text.split()  # one box can hold several words; they share its width
        step = (x1 - x0) / max(1, len(text))
        pos = x0
        for part in parts:
            words.append(Word(part, pos, pos + step * len(part), (top + bottom) / 2, bottom - top))
            pos += step * (len(part) + 1)
    return words


# ----------------------------------------------------------------------------------------------------
# Lines, segments, columns
# ----------------------------------------------------------------------------------------------------
def _is_amount(text: str) -> bool:
    return bool(_AMOUNT.match(european_to_comma(text.replace("Rs.", "")))) or text.lower() in _DASHES


_MONTH = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.,]?$", re.I)


def _date_line(line: list[Word]) -> bool:
    """A heading row of dates: "31 March 2025   31 March 2024", "2025 2024 2023", "FY 2024-25"."""
    numbers = [w.text for w in line if _is_amount(w.text)]
    if not numbers:
        return False
    month = any(_MONTH.match(w.text) for w in line)
    return all(re.fullmatch(r"(19|20)\d\d", n) or (month and re.fullmatch(r"\d{1,2},?", n)) for n in numbers)


def _amount(text: str) -> float | None:
    if text.lower() in _DASHES:
        return None
    return parse_number(text.replace("Rs.", "").replace("₹", "").replace("$", ""))


def _lines(words: list[Word]) -> list[list[Word]]:
    heights = sorted(w.height for w in words if w.height)
    tol = max(3.0, 0.45 * heights[len(heights) // 2]) if heights else 3.0
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(lines[-1][0].top - w.top) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


@dataclass
class _Segment:
    label: list[Word]
    amounts: list[Word]
    sentence: bool = False


def _segments(line: list[Word]) -> list[_Segment]:
    segs = [_Segment([], [])]
    prev: Word | None = None
    for w in line:
        cur = segs[-1]
        low = w.text.lower().strip(".,")
        if prev is None and re.fullmatch(r"\(\d{1,2}\)|\d{1,2}\.", w.text):
            cur.label.append(w)  # "(2) Non-current liabilities": the item number opens the label
        elif _is_amount(w.text):
            cur.amounts.append(w)
        elif low in _UNITS:
            pass  # "Rs. 1,234.5 Cr"
        elif w.text.endswith("%"):
            if not cur.amounts:
                cur.sentence = True  # "grew 12% to 5,400": a percentage in a sentence
            # after the amounts it is a "% Change" column: never an amount, and no sentence either
        else:
            if cur.amounts and low in ("and", "or", "&") and not cur.sentence:
                cur.label.extend(cur.amounts)  # amounts quoted inside the label, not the row's figures
                cur.amounts = []
                cur.label.append(w)
                prev = w
                continue
            if cur.amounts:
                if prev is not None and w.x0 - prev.x1 > 4 * CHAR_W:
                    segs.append(_Segment([w], []))  # a second table on the same line
                    prev = w
                    continue
                cur.sentence = True  # words straight after an amount: narrative
            cur.label.append(w)
        prev = w
    return segs


def _columns(rights: list[float]) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for x in sorted(rights):
        if spans and x - spans[-1][1] <= 2.5 * CHAR_W:
            spans[-1][1] = x
        else:
            spans.append([x, x])
    return [(a, b) for a, b in spans]


def _phrases(line: list[Word]) -> list[list[Word]]:
    """Words close together; a gap wider than two characters starts a new phrase."""
    out: list[list[Word]] = []
    for w in line:
        if out and w.x0 - out[-1][-1].x1 <= 2 * CHAR_W:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _heading_parts(line: list[Word]) -> list[list[Word]]:
    """Phrases, except that years and dates stand alone: "In thousands 2025 2024 2023" heads three columns."""
    out: list[list[Word]] = []
    for phrase in _phrases(line):
        run: list[Word] = []
        for w in phrase:
            if _YEAR.search(w.text) or re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", w.text):
                if run:
                    out.append(run)
                    run = []
                out.append([w])
            else:
                run.append(w)
        if run:
            out.append(run)
    return out


def _column_of(cols: list[tuple[float, float]], x: float) -> int | None:
    for i, (a, b) in enumerate(cols):
        if a - 2 * CHAR_W <= x <= b + 2 * CHAR_W:
            return i
    return None


def _is_note_column(header: str, values: list[float]) -> bool:
    # "Note" / "Notes" / "Note No." heads a short column; "The accompanying notes are an integral part" does not.
    words = header.lower().split()
    named = any(re.fullmatch(r"notes?\.?", w) for w in words)
    small = sum(1 for v in values if v.is_integer() and 0 < v <= 99) >= max(1, 0.6 * len(values))
    # Unnamed: nearly all small whole numbers ("25", "26", ... with a "34.07" sub-note among them).
    mostly = len(values) >= 3 and sum(1 for v in values if v.is_integer() and 0 < v <= 60) >= 0.8 * len(values)
    return (named and (len(words) <= 6 or small)) or mostly


_ENTITY_WORDS = {"group": "consolidated", "consolidated": "consolidated", "company": "standalone",
                 "standalone": "standalone"}


def other_period(headers: dict[int, str], best: int | None, col: int) -> bool:
    """True when ``col`` is headed by another year than the chosen column ``best``.

    Bug hunt (8 November 2026): a row with no figure in this year's column ("Short-term borrowings | - | 300")
    took its first other amount, last year's 300, as this year's.
    """
    def year(header: str) -> int | None:
        years = [int(m.group(1)) for m in _YEAR.finditer(header or "")]
        return max(years) if years else None

    chosen, other = year(headers.get(best, "")) if best is not None else None, year(headers.get(col, ""))
    return chosen is not None and other is not None and chosen != other


def _entities(headers: dict[int, str]) -> dict[int, str]:
    """Which entity each column is ("standalone" or "consolidated"), when the page has both.

    Held-out set (Kotagala Plantations): "Group" and "Company" are centred over pairs of columns ("2025 2024"), so
    each word lands on one column of its pair. A column with no entity word takes the one of its nearest labelled
    column (to the right on a tie, where the heading of a pair sits over its second column).
    """
    labelled = {}
    for col, h in headers.items():
        found = {v for k, v in _ENTITY_WORDS.items() if re.search(r"\b" + k + r"\b", h.lower())}
        if len(found) == 1:
            labelled[col] = found.pop()
    if len(set(labelled.values())) < 2:
        return {}
    return {col: labelled.get(col) or min(labelled.items(), key=lambda kv: (abs(kv[0] - col), -kv[0]))[1]
            for col in headers}


def choose_column(headers: dict[int, str], values: dict[int, list[float]],
                  entity: str = "STANDALONE") -> int | None:
    """The current annual column of the bank's entity (standalone unless set), from the header words above it."""
    fullest = max((len(v) for v in values.values()), default=0)
    entities = _entities(headers)
    scored: list[tuple[float, int, int]] = []
    dated: dict[int, tuple[int, int]] = {}
    top_year: dict[int, int] = {}
    # An interim page heads its current columns "Unaudited" and the last year-end "Audited" (Teejay Lanka): there
    # "audited" marks the old balance sheet, not the year's figures.
    interim = any(re.search(r"\bunaudited\b", h.lower()) for h in headers.values())
    col_months = column_months(headers)
    for col, vals in values.items():
        h = headers.get(col, "").lower()
        if _is_note_column(h, vals):
            continue
        if len(vals) < 0.25 * fullest:
            continue  # a stray number (a membership number under the signatures) is no column of figures
        score = 0.0
        # "Twelve months ended" is the year; three, six or nine months, quarters and halves are not.
        # Sri Lanka run (Watawala): "Six months ended" spread over two columns left the second one only "months".
        if re.search(r"quarter|qtr|\bq[1-4]\b|half|\b(three|six|nine|3|6|9)\s+months|\bmonth\s+ended", h) or (
                re.search(r"\bmonths\b", h) and not re.search(r"twelve|12\s+months", h)) or (
                col_months.get(col) in (3, 6, 9)):
            score -= 50
        if re.search(r"year|\bfy|annual", h) or (re.search(r"\baudited", h) and not interim):
            score += 5
        # Only against a column of the other entity; a page title "Consolidated statements" is no penalty. Sri
        # Lankan "Group | Company" columns: the company is the standalone entity.
        if entities.get(col) and entities[col] != entity.lower():
            score -= 20
        if "previous" in h or "prior" in h:
            score -= 30
        years = [int(a) + (1 if b else 0) for a, b in _YEAR.findall(h)]
        years += [2000 + int(y) for y in _SHORT_DATE.findall(h)]  # "30-Sep-25"
        # "Change + / (-)": the difference between two columns. A "Change %" heading over a column of percentages
        # (no figures) lands on its neighbour, which is headed by its own year (Watawala): that one is no change.
        if re.search(r"\bchange\b|variance|increase|decrease", h) and len(set(years)) != 1:
            score -= 50
        if years:
            score += (max(years) - 2000) * 10
            top_year[col] = max(years)
        when = _month_of(h)
        if when:
            dated[col] = when
        months = col_months.get(col)
        if months in (3, 6, 9) or (months is None and re.search(r"\bmonths\b|\bperiod\b", h)):
            # 12-month covenants (10 November 2026): the year to date, not the quarter, is this period's
            # figure; a year to date is what the trailing twelve months are built from.
            score += months if months else 4.5
        scored.append((score, -col, col))
    if not scored:
        return None
    # An interim page that also prints the last full year ("Year to 31/03/2026 Audited | 03 months to 30/06/2026",
    # Dipped Products): the full year is the last year-end, not this period. The year to date is.
    part_dates = [dated[c] for _, _, c in scored if c in dated and col_months.get(c) in (3, 6, 9)]
    if part_dates:
        later = max(part_dates)
        scored = [(s - 60 if col_months.get(c) == 12 and dated.get(c, later) < later else s, n, c)
                  for s, n, c in scored]
    # Sri Lanka run: "30-Jun-26 | 31-Mar-26" or "30 September 2026 | 31 March 2026 (Audited)" share one year, and the
    # earlier (audited year-end) column was taken. Within the year, the latest date is this period's column.
    # Only when every column of that year names its month: Teejay Lanka's "30 September" heading landed on the 2025
    # columns, and "2026" alone stood over this year's.
    latest = max((dated[c] for _, _, c in scored if c in dated), default=None)
    same_year = [c for _, _, c in scored if top_year.get(c) == (latest or (0,))[0]]
    if latest and len(same_year) > 1 and all(c in dated for c in same_year):
        scored = [(s + (6 if dated.get(c) == latest else 0), n, c) for s, n, c in scored]
    return max(scored)[2]


_PERIOD_WORDS = [
    (re.compile(r"\b(?:twelve|12)\s+months|\byear\s+(?:ended|to|ending)|\bfull\s+year|\bannual\b|\bfy\b", re.I), 12),
    (re.compile(r"\b(?:nine|0?9)\s+months", re.I), 9),
    (re.compile(r"\b(?:six|0?6)\s+months|\bhalf[- ]?year|\bh1\b", re.I), 6),
    (re.compile(r"\b(?:three|0?3)\s+months|\bquarter\b|\bqtr\b|\bq[1-4]\b", re.I), 3),
]


def period_months(header: str) -> int | None:
    """How many months a column covers, from its heading: "Six months ended" 6, "Quarter ended" 3, "Year ended" 12."""
    for pattern, months in _PERIOD_WORDS:
        if pattern.search(header or ""):
            return months
    return None


def _header_year(header: str) -> int | None:
    h = (header or "").lower()
    ys = [int(a) + (1 if b else 0) for a, b in _YEAR.findall(h)] + [2000 + int(y) for y in _SHORT_DATE.findall(h)]
    return max(ys) if ys else None


def column_months(headers: dict[int, str]) -> dict[int, int | None]:
    """Each column's period in months. A heading such as "03 months ended 30 June" sits centred over its pair of
    columns (this year, last year) and lands on one of them (Ceylon Tobacco); the other column of the pair, its
    neighbour one year apart, takes the same period."""
    months = {c: period_months(h) for c, h in headers.items()}
    cols = sorted(headers)
    out = dict(months)
    for i, c in enumerate(cols):
        if months[c] is not None or _header_year(headers[c]) is None:
            continue
        for n in [cols[j] for j in (i + 1, i - 1) if 0 <= j < len(cols)]:
            y, yn = _header_year(headers[c]), _header_year(headers[n])
            if months[n] is not None and yn is not None and abs(y - yn) == 1:
                out[c] = months[n]
                break
    return out


def period_columns(headers: dict[int, str], values: dict[int, list[float]], best: int | None,
                   entity: str = "STANDALONE") -> tuple[int | None, int | None, int | None]:
    """The months the chosen column covers, last year's same-period column and the last full-year column.

    12-month covenants (10 November 2026): an interim statement prints last year's same period beside this one,
    and often the last full year; the trailing twelve months are built from the three.
    """
    if best is None:
        return None, None, None
    entities = _entities(headers)
    col_months = column_months(headers)

    def year(col: int) -> int | None:
        return _header_year(headers.get(col, ""))

    def usable(col: int) -> bool:
        h = headers.get(col, "").lower()
        return (col != best and not _is_note_column(h, values.get(col, []))
                and len(values.get(col, [])) >= 0.25 * max((len(v) for v in values.values()), default=0)
                and not (entities.get(col) and entities.get(col) != entities.get(best, entities.get(col)))
                and not (re.search(r"\bchange\b|variance", h) and year(col) is None))

    months, this_year = col_months.get(best), year(best)
    prior = annual = None
    if this_year is not None:
        same = [c for c in values if usable(c) and year(c) == this_year - 1 and col_months.get(c) == months]
        prior = min(same, key=lambda c: abs(c - best)) if same else None
    if months != 12:
        # The last full year ends within the twelve months before this period (Sunshine: last year's same period
        # was taken for it). Without both dates it is not used.
        now = _month_of(headers.get(best, "").lower())

        def recent(col: int) -> bool:
            end = _month_of(headers.get(col, "").lower())
            if end is None or now is None:
                return False
            gap = (now[0] - end[0]) * 12 + now[1] - end[1]
            return 0 < gap < 12

        full = [c for c in values if usable(c) and col_months.get(c) == 12 and recent(c)]
        annual = min(full, key=lambda c: abs(c - best)) if full else None
    return months, prior, annual


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}
_WORD_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/ .,]*(?:(20\d\d)|(\d\d))(?!\d)",
                        re.I)
_NUMERIC_DATE = re.compile(r"(?<!\d)(?:\d{1,2}[./-](\d{1,2})[./-](20\d\d|\d\d)"
                           r"|(20\d\d)[./-](\d{1,2})[./-]\d{1,2})(?!\d)")


def _month_of(header: str) -> tuple[int, int] | None:
    """The latest (year, month) a column header names: "30-Jun-26", "30 September 2026", "30.06.2026", "2026.09.30"."""
    found = []
    for m in _WORD_DATE.finditer(header):
        year = int(m.group(2)) if m.group(2) else 2000 + int(m.group(3))
        found.append((year, _MONTHS[m.group(1).lower()[:3]]))
    for m in _NUMERIC_DATE.finditer(header):
        if m.group(1):
            year = int(m.group(2)) if len(m.group(2)) == 4 else 2000 + int(m.group(2))
            month = int(m.group(1))
        else:
            year, month = int(m.group(3)), int(m.group(4))
        if 1 <= month <= 12:
            found.append((year, month))
    return max(found) if found else None


_MARKER = re.compile(r"\b(group|company|consolidated|standalone|quarter|year|months?)\b", re.I)


def _spread_markers(lines: list[list[Word]], segs: list[list[_Segment]], reach: dict[int, tuple[float, float]],
                    headers: dict[int, str]) -> None:
    """A heading over a pair of columns names both: "GROUP" over "2025 2024", "Quarter ended" over two dates.

    Only heading lines above the first row of figures, so a signature ("Company Secretary") names nothing.
    On a line with several such headings each column takes the nearest; a lone one covers what it overlaps.
    """
    for line, ss in zip(lines, segs, strict=True):
        if any(s.amounts and not s.sentence for s in ss) and not _date_line(line):
            return
        if any(s.sentence for s in ss) or len(line) > 12:
            continue  # narrative ("... against 4,800 last year") heads no column
        marks = [p for p in _phrases(line) if len(p) <= 5 and _MARKER.search(" ".join(w.text for w in p))]
        if not marks or not reach:
            continue
        for col, (a, b) in reach.items():
            centre = (a + b) / 2
            if len(marks) > 1:
                p = min(marks, key=lambda p: abs((p[0].x0 + p[-1].x1) / 2 - centre))
            else:
                p = marks[0]
                if min(p[-1].x1, b) - max(p[0].x0, a) <= 0:
                    continue
            text = " ".join(w.text for w in p)
            words = " ".join(m.group(0) for m in _MARKER.finditer(text))
            length = re.search(r"\b(?:three|six|nine|twelve|0?3|0?6|0?9|12)\s+months\b", text, re.I)
            if length:  # "Six months ended" names the period, not just "months" (12-month covenants)
                words = length.group(0) + " " + words
            if words.lower() not in headers.get(col, "").lower():
                headers[col] = (headers.get(col, "") + " " + words).strip()


def rows_from_words(pages: list[list[Word]], numbers: list[int] | None = None,
                    entity: str = "STANDALONE", periods: Periods | None = None) -> list[Row]:
    """Rows from each page's words; ``numbers`` are the pages' 1-based numbers in the file (default 1, 2, ...).

    ``periods`` (optional) collects each page's period in months and the rows of its comparative columns.
    """
    out: list[Row] = []
    for index, words in enumerate(pages):
        page_no = numbers[index] if numbers else index + 1
        lines = _lines(words)
        segs = [_segments(line) for line in lines]
        rights = [a.x1 for line, ss in zip(lines, segs, strict=True) if not _date_line(line)
                  for s in ss if not s.sentence for a in s.amounts]
        cols = _columns(rights)
        sizes = [sum(1 for x in rights if a <= x <= b) for a, b in cols]
        # Below the statement's last line come the signatures ("Date: 29th May, 2025" under the prior-year
        # column); they head no column.
        last = max((i for i, ss in enumerate(segs) for s in ss
                    if s.amounts and not s.sentence and match_key(" ".join(w.text for w in s.label))), default=-1)
        headers: dict[int, str] = {}
        # Headings go to columns of figures, not to a stray number's column ("31 March 2025" over one amount).
        reach = {i: (a - 14 * CHAR_W, b + CHAR_W) for i, (a, b) in enumerate(cols)
                 if sizes[i] >= 0.25 * max(sizes)}
        parsed: list[dict] = []
        section = ""
        changes = False  # inside "Changes in operating assets and liabilities" of a cash flow statement
        total_for = ""  # the total an unlabelled figure stands for, after a "Current assets" heading
        for index, (line, ss) in enumerate(zip(lines, segs, strict=True)):
            if _date_line(line) or not any(s.amounts and not s.sentence for s in ss):
                text = " ".join(w.text for w in line).strip()
                # a row of dates is a heading; a sentence quoting amounts is not
                if (_date_line(line) or not any(_is_amount(w.text) for w in line)) and (index < last or last < 0):
                    for phrase in _heading_parts(line):  # a heading goes to the column it overlaps most
                        if any("%" in w.text for w in phrase):
                            continue  # "% Change" heads percentages, which are never read as amounts
                        x0, x1 = phrase[0].x0, phrase[-1].x1
                        overlap = {i: min(x1, b) - max(x0, a) for i, (a, b) in reach.items()}
                        best_col = max(overlap, key=overlap.get, default=None)
                        if len(phrase) == 1 and _YEAR.search(phrase[0].text) and reach:
                            # A year sits right-aligned or centred over its own column: nearest edge or centre.
                            mid = (x0 + x1) / 2
                            best_col = min(reach, key=lambda i: min(abs(x1 - cols[i][1]),
                                                                               abs(mid - sum(cols[i]) / 2)))
                            overlap[best_col] = max(overlap.get(best_col, 0), 1)
                        over = [i for i, (a, b) in enumerate(cols) if min(x1, b) > max(x0, a - 2 * CHAR_W)]
                        if len(phrase) > 1 and len(over) > 1:
                            continue  # a title across several columns ("Years ended December 31, 2025, 2024")
                        if best_col is not None and overlap[best_col] > 0:
                            words_text = " ".join(w.text for w in phrase)
                            headers[best_col] = (headers.get(best_col, "") + " " + words_text).strip()
                if parsed and parsed[-1]["open"] and text[:1].islower():
                    parsed[-1]["label"] += " " + text  # a label wrapped onto the next line
                    parsed[-1]["open"] = False
                elif any(rx.search(text) for rx, _ in _SECTIONS):
                    section = text
                block = _BLOCK.match(text)
                if block:
                    total_for = f"Total {block.group(2).lower()} {block.group(3).lower()}"
                if _CHANGES.search(text):
                    changes = True
                elif re.search(r"investing|financing", text, re.I):
                    changes = False
                continue
            for s in ss:
                if s.sentence or not s.amounts:
                    continue
                vals: dict[int, float | None] = {}
                for a in s.amounts:
                    c = _column_of(cols, a.x1)
                    if c is not None and c not in vals:
                        vals[c] = _amount(a.text)
                label = " ".join(w.text for w in s.label).strip(" .:-")
                if not label and total_for:
                    # Sri Lankan and many Indian statements print the block's total with no words beside it:
                    # the first unlabelled figure under "Current Assets" is total current assets.
                    label, total_for = total_for, ""
                elif label.lower().startswith("total"):
                    total_for = ""  # the block printed its own total
                if re.match(r"(net\s+)?cash\s+(generated|provided|used|from|flows?)|net\s+cash", label, re.I):
                    changes = False
                elif changes:
                    label = "change in " + label  # "Accounts receivable (1,532.9)" there is a movement, not a balance
                parsed.append({"label": label, "vals": vals, "section": section, "open": True})
        by_col: dict[int, list[float]] = {}
        for r in parsed:
            for c, v in r["vals"].items():
                if v is not None:
                    by_col.setdefault(c, []).append(v)
        _spread_markers(lines, segs, reach, headers)
        best = choose_column(headers, by_col, entity)
        if periods is not None and best is not None:
            months, prior, annual = period_columns(headers, by_col, best, entity)
            periods.months[page_no] = months
            for col, into in ((prior, periods.prior), (annual, periods.annual)):
                if col is None:
                    continue
                into.extend((r["label"], r["vals"][col], page_no, None) for r in parsed
                            if r["label"] and r["vals"].get(col) is not None)
            if annual is not None and periods.annual_end is None:
                periods.annual_end = _month_of(headers.get(annual, "").lower())
        for r in parsed:
            label = r["label"]
            if _BARE_BORROWINGS.fullmatch(label) and r["section"]:
                label = next(name for rx, name in _SECTIONS if rx.search(r["section"]))
            value = r["vals"].get(best) if best is not None else None
            if value is None and best is not None and best in r["vals"]:
                value = 0.0  # a dash in this year's column is nil this year, not last year's figure
            elif value is None:
                # A row outside the chosen column (side-by-side tables): its own first non-note amount, never
                # one headed by another year.
                own = [c for c in sorted(r["vals"]) if r["vals"][c] is not None
                       and not _is_note_column(headers.get(c, ""), by_col.get(c, []))
                       and not other_period(headers, best, c)]
                value = r["vals"][own[0]] if own else None
            if label and value is not None:
                out.append((label, value, page_no, None))
    return out


def _compact(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def ocr_label(label: str) -> str:
    """OCR often drops spaces ("Totalcurrentassets"); match the catalog phrases without spaces."""
    if match_key(label):
        return label
    c = _compact(label)
    best = ""
    for phrases in LINE_ALIASES.values():
        for phrase in phrases:
            p = _compact(phrase)
            if len(p) >= 4 and c.startswith(p) and len(p) > len(_compact(best)) and len(p) >= 0.6 * len(c):
                best = phrase
    return best or label


# ----------------------------------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------------------------------
def unit_text(text: str) -> str:
    """The part of a page that states its unit: the text under the heading, or a footnote when that has none."""
    from dps.extraction.statement import _UNIT_HINT, _quotes

    head = text[:1500]
    if _UNIT_HINT.search(_quotes(head)):
        return head  # a scale under the heading; a bare "LKR" over the columns (Hemas) still looks for the note
    note = _UNIT_NOTE.search(text[1500:])
    return head + " " + note.group(0) if note else head


def read_pdf_statement(data: bytes, ocr=None, dpi: int = 200, entity: str = "STANDALONE") -> PdfStatement:
    """Rows from a statement PDF. ``ocr`` is a RapidOcrBackend (or None: scanned pages give no rows).

    A long report (an annual report, a 10-K) is searched first and only its statement pages are read, the
    best balance sheet / P&L / cash flow page before the runners-up (extraction.statement_pages).
    """
    import pdfplumber

    from dps.extraction.statement import _unit, unit_stated
    from dps.extraction.statement_pages import statement_pages

    selection = statement_pages(data, ocr, entity)
    chosen, main = selection if selection is not None else (None, 0)
    main_unit = None
    pages: list[list[Word]] = []
    numbers: list[int] = []
    texts: list[str] = []
    header, ocr_pages, confidences = "", [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for position, index in enumerate(chosen if chosen is not None else range(len(pdf.pages))):
            page = pdf.pages[index]
            text = page.extract_text() or ""
            if chosen is not None and position == main:
                main_unit = _unit(header)
            if main_unit and unit_stated(text[:1500]) and _unit(text[:1500]) != main_unit:
                continue  # a runner-up in another unit ("Rs 000'" in a ten-year summary): not these statements
            # A broken font encoding ("(cid:12)") gives text no reader can use: read the page image instead.
            if len(re.findall(r"\w", text)) >= MIN_TEXT_CHARS and len(_GARBLE.findall(text)) < 5:
                words = _text_words(page)
            elif ocr is not None:
                boxes = ocr.ocr_boxes(data, index, dpi)
                words = ocr_words(boxes, dpi)
                text = " ".join(w.text for w in words)
                ocr_pages.append(index + 1)
                if boxes:
                    confidences.append(sum(float(b[2]) for b in boxes) / len(boxes))
            else:
                words = []
            pages.append(words)
            numbers.append(index + 1)
            texts.append(text)
            # The unit is stated under each statement's heading, sometimes well down the page.
            header += unit_text(text) + " "
    periods = Periods()
    rows = rows_from_words(pages, numbers, entity, periods)
    if ocr_pages:
        def relabel(rs: list[Row]) -> list[Row]:
            return [(ocr_label(r[0]) if r[2] in ocr_pages else r[0], *r[1:]) for r in rs]

        rows, periods.prior, periods.annual = relabel(rows), relabel(periods.prior), relabel(periods.annual)
    quality = round(sum(confidences) / len(confidences), 3) if confidences else None
    return PdfStatement(rows, header, ocr_pages, quality, texts, numbers, periods)
