"""Grounding checks for an AI reader: a figure or a covenant the model reports counts only if it is on the page.

A language model can return a number that is not in the document (a sum it did itself, a previous year, a guess).
Phase 2 of "never miss silently" uses the model as a second reader, so every answer it gives is checked here
against the text DPS itself read: the number must be printed on a line of the cited page that carries the
label, and a covenant's quote must be on the cited page (or the one next to it) and contain its threshold.
An answer that fails is dropped, never stored and never used to overrule the rules.
"""

from __future__ import annotations

import re

_NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_WORD = re.compile(r"[a-z]{3,}")
_SPACED = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")  # "1 200" is 1200; "2025 2024" stays two
STOP = {"the", "and", "for", "from", "total", "net", "less", "with", "other", "year", "ended", "per"}


def normalize(text: str) -> str:
    """Lower case, one space, typographic quotes and dashes made plain (pdfplumber and OCR differ in these), and every
    script's digits as 0-9 (Phase 3b-2: an Arabic or Hindi quote is checked like an English one)."""
    from dps.language import plain_digits

    text = plain_digits(text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"')
    text = text.replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def numbers_in(text: str) -> list[float]:
    """Every number in the text. A comma before one or two final digits is a decimal mark ("1,25" in French or
    Spanish is 1.25); any other comma groups thousands ("1,250"). A space between groups of three digits may group
    thousands too ("1 200,0" in French) or part a note number from the amount ("Finance costs 30 781.47"): both
    readings are given."""
    from dps.language import plain_digits

    out = []
    plain = plain_digits(text or "")
    merged = _SPACED.sub("", plain)
    for raw in _NUM.findall(plain) + (_NUM.findall(merged) if merged != plain else []):
        digits = raw.strip("()").lstrip("-")
        digits = digits.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", digits) else digits.replace(",", "")
        try:
            out.append(float(digits))
        except ValueError:
            continue
    return out


def _close(a: float, b: float) -> bool:
    # The model copies a printed number: it must match to the printed digits ("16,363.10" = 16363.1), not
    # merely be near (17,245.00 is not 17,247.45).
    return abs(a - b) <= max(0.006, 1e-7 * abs(b))


def number_on_line(page_text: str, value: float, label: str) -> bool:
    """The value (as printed, any sign) on one line of the page that also carries most of the label's words."""
    words = {w for w in _WORD.findall(normalize(label)) if w not in STOP}
    for line in (page_text or "").splitlines():
        if not any(_close(n, abs(value)) for n in numbers_in(line)):
            continue
        if not words:
            return True
        on_line = set(_WORD.findall(normalize(line)))
        if len(words & on_line) >= max(1, (len(words) + 1) // 2):
            return True
    return False


def quote_on_page(pages: dict[int, str], page: int | None, quote: str) -> int | None:
    """The page (cited, or the one before or after it) whose text holds the quote; None when no page does.

    The quote is compared word for word after normalising spaces and case; a quote of fewer than five words
    proves nothing and is refused.
    """
    q = normalize(quote).strip(" .;,\"'")
    if len(q.split()) < 5:
        return None
    candidates = [page, page - 1, page + 1] if isinstance(page, int) else []
    for p in candidates + [p for p in pages if p not in candidates]:
        if p in pages and q in normalize(pages[p]):
            return p
    return None


def threshold_in(quote: str, threshold: float) -> bool:
    """The covenant's number is written in its own quote (as digits: "1.25", "125%", "3.50:1.00").

    The "1" of a ratio ("3.50 to 1.00", "1.25:1") is not a threshold, so a model answering 1.0 is not grounded by it.
    """
    text = re.sub(r"(?:\bto|:)\s*1(?:\.0+)?(?![\d.])", " ", quote or "")
    return any(_close(n, threshold) for n in numbers_in(text))
