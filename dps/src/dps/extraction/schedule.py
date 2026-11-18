"""Phase 3b-1 (11 November 2026): the parts of a financial covenant beyond its first level.

- **Step-down schedule**: "4.00 to 1.00 for any Test Period ending on or before 31 March 2027 and 3.50 to 1.00
  thereafter", or a table of test dates and levels. Each level gets the period it applies to.
- **Test dates**: "tested on 31 March and 30 September of each year": the months the covenant is tested in.
- **Cure right**: "the Borrower may cure ... by an equity contribution within 15 Business Days ... not more than
  two times": the kind, the days and the times allowed.

Everything here is read by rules from the agreement's words, never guessed; a person sees it before Apply.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"])}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|" \
       r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
# "31 March 2027", "31st March, 2027", "March 31, 2027", "31.03.2027", "31/03/2027", "2027-03-31"
DATE = re.compile(
    r"\b(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>" + _MON + r")\.?,?\s+(?P<y1>(?:19|20)\d\d)"
    r"|(?P<m2>" + _MON + r")\.?\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>(?:19|20)\d\d)"
    r"|(?P<d3>\d{1,2})[./](?P<m3>\d{1,2})[./](?P<y3>(?:19|20)\d\d)"
    r"|(?P<y4>(?:19|20)\d\d)-(?P<m4>\d{2})-(?P<d4>\d{2}))\b", re.I)
_LEVEL = r"(?P<v>\d+(?:\.\d+)?)\s*(?:x\b|times\b|:\s*1(?:\.0+)?(?![\d.])|to\s*1(?:\.0+)?(?![\d.]))"
_BEFORE = r"(?:on\s+or\s+(?:before|prior\s+to)|up\s+to(?:\s+and\s+including)?|until|through|till|prior\s+to|before)"
_FROM = r"(?:on\s+or\s+after|after|from|commencing(?:\s+on)?|beginning(?:\s+on)?)"
_THEREAFTER = re.compile(r"\b(?:thereafter|at\s+all\s+times\s+thereafter|each\s+(?:fiscal\s+)?quarter\s+thereafter"
                         r"|for\s+each\s+(?:test\s+period|quarter)\s+thereafter)\b", re.I)


@dataclass(frozen=True)
class Level:
    threshold: float
    from_date: str | None  # ISO date; None: from the start
    to_date: str | None  # ISO date; None: for the rest of the loan

    def as_dict(self) -> dict:
        return {"threshold": self.threshold, "from": self.from_date, "to": self.to_date}


@dataclass(frozen=True)
class Cure:
    kind: str  # EQUITY | REMEDY
    days: int | None
    business_days: bool
    max_uses: int | None
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


def parse_date(m: re.Match) -> date | None:
    g = m.groupdict()
    try:
        if g["y1"]:
            return date(int(g["y1"]), _MONTHS[g["m1"].lower()[:4].rstrip(".") if g["m1"].lower()[:4] == "sept"
                                              else g["m1"].lower()[:3]], int(g["d1"]))
        if g["y2"]:
            return date(int(g["y2"]), _MONTHS[g["m2"].lower()[:3]], int(g["d2"]))
        if g["y3"]:
            return date(int(g["y3"]), int(g["m3"]), int(g["d3"]))  # day first, as in India, Sri Lanka and the UK
        return date(int(g["y4"]), int(g["m4"]), int(g["d4"]))
    except (KeyError, ValueError):
        return None


def step_down(text: str, first: float | None) -> list[Level]:
    """The levels of a step-down schedule in ``text`` (the clause and what follows it), oldest first; empty when
    the text gives one level only. ``first`` is the level the rules read: the schedule must contain it."""
    levels = _sentence_levels(text) or _table_levels(text)
    if len(levels) < 2 or (first is not None and all(abs(lv.threshold - first) > 1e-9 for lv in levels)):
        return []
    return levels


def _sentence_levels(text: str) -> list[Level]:
    """"4.00 to 1.00 for any Test Period ending on or before 31 March 2027, 3.75 to 1.00 for any Test Period ending
    after 31 March 2027 and on or before 31 March 2028 and 3.50 to 1.00 thereafter"."""
    out: list[tuple[float, date | None, date | None]] = []
    # A level's words run to the next level, a ";" or the end of the sentence.
    pattern = re.compile(_LEVEL + r"(?P<tail>[^;]{0,200}?)(?=" + _LEVEL.replace("?P<v>", "?:") + r"|;|\.(?:\s|$)|$)",
                         re.I | re.S)
    for m in pattern.finditer(text):
        value, tail = float(m.group("v")), m.group("tail")
        dates = [(d, parse_date(d)) for d in DATE.finditer(tail)]
        until = next((dt for d, dt in dates if re.search(_BEFORE + r"\s*$", tail[:d.start()], re.I)), None)
        start = next((dt for d, dt in dates if re.search(_FROM + r"\s*$", tail[:d.start()], re.I)), None)
        if until or start:
            out.append((value, start, until))
        elif _THEREAFTER.search(tail) and out:
            out.append((value, None, None))
    return _chain(out)


def _table_levels(text: str) -> list[Level]:
    """A table with one test date and one level per line ("March 31, 2027   4.00:1.00"); the last level holds after
    the last date when the table says "and thereafter"."""
    rows = []
    for line in text.splitlines():
        d = DATE.search(line)
        v = re.search(_LEVEL, line, re.I)
        if d and v and parse_date(d):
            rows.append((parse_date(d), float(v.group("v")), bool(_THEREAFTER.search(line))))
    if len(rows) < 2 or any(rows[i][0] >= rows[i + 1][0] for i in range(len(rows) - 1)):
        return []
    out = []
    prev = None
    for i, (end, value, after) in enumerate(rows):
        last = i == len(rows) - 1
        out.append(Level(value, (prev + timedelta(days=1)).isoformat() if prev else None,
                         None if last and (after or _THEREAFTER.search(text)) else end.isoformat()))
        prev = end
    return _merge(out)


def _chain(found: list[tuple[float, date | None, date | None]]) -> list[Level]:
    """Gives each sentence level its period: one ends the day before the next starts, "thereafter" has no end."""
    out = []
    prev_end: date | None = None
    for value, start, until in found:
        # A level after the first starts the day after the one before it ends ("after 31 March 2027": 1 April).
        frm = prev_end + timedelta(days=1) if prev_end is not None else start
        if until is None and start is not None and frm is not None and out and out[-1].to_date is None:
            break
        out.append(Level(value, frm.isoformat() if frm else None, until.isoformat() if until else None))
        prev_end = until
        if until is None:
            break
    dates_ok = all(lv.to_date is None or lv.from_date is None or lv.from_date <= lv.to_date for lv in out)
    return _merge(out) if dates_ok else []


def _merge(levels: list[Level]) -> list[Level]:
    """Neighbouring rows with the same level are one period."""
    out: list[Level] = []
    for lv in levels:
        if out and abs(out[-1].threshold - lv.threshold) < 1e-9:
            out[-1] = Level(lv.threshold, out[-1].from_date, lv.to_date)
        else:
            out.append(lv)
    return out


_TESTED = re.compile(r"\btested\b|\btest\s+dates?\b|\bcalculation\s+dates?\b|\bquarter\s+dates?\b|\bmeasured\b"
                     r"|\btesting\s+dates?\b", re.I)
_MONTH_DAY = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MON + r")|(" + _MON + r")\s+(\d{1,2}))\b"
                        r"(?!,?\s+(?:19|20)\d\d)", re.I)


def test_months(text: str) -> list[int]:
    """The months a covenant is tested in ("tested on 31 March and 30 September in each year": [3, 9]); empty when
    the text names no recurring test date."""
    m = _TESTED.search(text or "")
    if not m:
        return []
    window = text[m.start():m.start() + 220]
    months = []
    for d in _MONTH_DAY.finditer(window):
        name = (d.group(2) or d.group(3)).lower()
        n = _MONTHS.get(name[:4].rstrip(".") if name.startswith("sept") else name[:3])
        if n and n not in months:
            months.append(n)
    return sorted(months) if len(months) >= 1 and re.search(r"\b(each|every)\s+(financial\s+|fiscal\s+)?year\b|"
                                                            r"\bannually\b|\bin\s+each\b", window, re.I) else []


def frequency_for(months: list[int]) -> str | None:
    """Core's schedule for the test months: one a year YEARLY, four evenly apart QUARTERLY, else CUSTOM."""
    if len(months) == 1:
        return "YEARLY"
    if len(months) == 4 and all((months[i + 1] - months[i]) == 3 for i in range(3)):
        return "QUARTERLY"
    return "CUSTOM" if months else None


def cron_for(months: list[int]) -> str | None:
    """Checked at 02:00 on the first day of the month after each test date (the statement is due by then or after;
    a statement that arrives later re-checks the covenant at once)."""
    if not months:
        return None
    after = sorted({m % 12 + 1 for m in months})
    return f"0 0 2 1 {','.join(str(m) for m in after)} ?"


_CURE = re.compile(r"\b(?:equity\s+cure|cure\s+(?:right|period|amount)|may\s+cure|right\s+to\s+cure"
                   r"|to\s+(?:remedy|cure)\s+(?:such|the|any)\s+(?:breach|default|non-?compliance|event)"
                   r"|be\s+(?:cured|remedied))\b", re.I)
_WITHIN = re.compile(r"within\s+(?P<n>\d{1,3}|[a-z]+(?:[\s-][a-z]+)?)\s*(?:\(\d{1,3}\)\s*)?"
                     r"(?P<b>business\s+|banking\s+|working\s+)?days?\b", re.I)
_TIMES = re.compile(r"(?:not\s+(?:be\s+exercised\s+|be\s+used\s+)?more\s+than|no\s+more\s+than|at\s+most"
                    r"|a\s+maximum\s+of|up\s+to|maximum\s+of)\s+"
                    r"(?P<n>\d{1,2}|one|two|three|four|five)\s*(?:\(\d\)\s*)?(?:times|occasions|cures)\b"
                    r"|(?:only\s+)?(?P<once>once|twice)\b", re.I)
_STOP = re.compile(r"(?<!\d)\.(?!\d)|\n\s*\n")  # a full stop, not the dot of "21.2" or "1.25"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "fifteen": 15,
          "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90, "forty-five": 45, "forty five": 45,
          "twenty-one": 21, "twenty one": 21, "once": 1, "twice": 2}


def _count(raw: str) -> int | None:
    raw = raw.lower().strip()
    return int(raw) if raw.isdigit() else _WORDS.get(raw)


def cure(text: str) -> Cure | None:
    """The borrower's right to cure a financial covenant breach, as the agreement words it, or None."""
    sentences = []
    for m in _CURE.finditer(text or ""):
        stops = [s.end() for s in _STOP.finditer(text, 0, m.start())]
        start = stops[-1] if stops else 0
        end = _STOP.search(text, m.end())
        sentences.append(" ".join(text[start:end.end() if end else m.end() + 400].split())[:600])
    if not sentences:
        return None
    # A heading ("21.3 Equity cure.") names the right; the sentence that gives its days or count is the one to keep.
    sentence = next((s for s in sentences if _WITHIN.search(s) or _TIMES.search(s)), sentences[0])
    within = _WITHIN.search(sentence)
    times = _TIMES.search(sentence)
    equity = re.search(r"equity|subordinated|shareholder|capital\s+contribution", sentence, re.I)
    kind = "EQUITY" if equity else "REMEDY"
    return Cure(kind, _count(within.group("n")) if within else None, bool(within and within.group("b")),
                _count(times.group("n") or times.group("once")) if times else None, sentence)


# ---------------------------------------------------------------- the agreement's own definitions and basis

# The figures each covenant family is built from, and the defined words an agreement uses for them.
INPUTS = {
    "DEBT_TO_EBITDA": ("EBITDA", "DEBT"), "NET_DEBT_TO_EBITDA": ("EBITDA", "DEBT"), "LEVERAGE": ("EBITDA", "DEBT"),
    "ICR": ("EBITDA", "INTEREST"), "INTEREST_COVERAGE": ("EBITDA", "INTEREST"), "DSCR": ("EBITDA", "INTEREST"),
    "FCCR": ("EBITDA", "INTEREST"), "MIN_EBITDA": ("EBITDA",), "DEBT_TO_EQUITY": ("DEBT", "NET_WORTH"),
    "DEBT_TO_NW": ("DEBT", "NET_WORTH"), "GEARING": ("DEBT", "NET_WORTH"), "MIN_NET_WORTH": ("NET_WORTH",),
    "MIN_TANGIBLE_NET_WORTH": ("NET_WORTH",),
}
_DEFINED = {
    "EBITDA": r"(?:consolidated\s+|adjusted\s+)*EBITDA",
    "DEBT": r"(?:consolidated\s+|total\s+|net\s+)*(?:debt|financial\s+indebtedness|borrowings)",
    "NET_WORTH": r"(?:consolidated\s+|tangible\s+)*net\s+worth",
    "INTEREST": r"(?:consolidated\s+|net\s+)*(?:interest\s+expense|finance\s+costs?|interest\s+payable)",
}
_QUOTES = "\"\u201c\u201d'\u2018\u2019"
# Wording that makes an agreement's figure differ from the statement's: added back, left out, capped, pro forma.
_ADJUSTED = re.compile(r"\badd(?:ed|ing)?\s+back\b|\bexclud(?:e|es|ed|ing)\b|\bexclusive\s+of\b|\bdisregard"
                       r"|\bother\s+than\b|\bnon[-\s]?cash\b|\bexceptional\b|\bextraordinary\b|\bone[-\s]?off\b"
                       r"|\bpro\s+forma\b|\bcapped\b|\bnot\s+exceed(?:ing)?\b|\brun[-\s]?rate\b|\bsynerg", re.I)
_GROUP = re.compile(r"\bon\s+a\s+consolidated\s+basis\b|\bconsolidated\s+(?:group|financial\s+statements)\b"
                    r"|\bof\s+the\s+group\b|\bgroup\s+basis\b", re.I)
_COMPANY = re.compile(r"\bon\s+a\s+(?:standalone|stand-alone|company|solo|unconsolidated)\s+basis\b"
                      r"|\bstandalone\s+financial\s+statements\b", re.I)


def definitions(text: str) -> dict[str, str]:
    """Each input's definition as the agreement gives it ("'EBITDA' means ..."), first 600 characters."""
    out = {}
    for key, name in _DEFINED.items():
        m = re.search(r"[" + _QUOTES + r"]\s*" + name + r"\s*[" + _QUOTES + r"]\s+(?:means|shall\s+mean|is)\b",
                      text or "", re.I)
        if m:
            out[key] = " ".join(text[m.start():m.end() + 600].split())
    return out


def adjustments(definition: str) -> list[str]:
    """The adjusting words of a definition ("add back", "excluding", "pro forma"), each with a little context."""
    seen = []
    for m in _ADJUSTED.finditer(definition or ""):
        lo = definition.rfind(" ", 0, max(0, m.start() - 30)) + 1  # whole words only
        hi = definition.find(" ", m.end() + 50)
        snippet = " ".join(definition[lo:hi if hi > 0 else len(definition)].split())
        if snippet not in seen:
            seen.append(snippet)
        if len(seen) >= 3:
            break
    return seen


def basis(quote: str) -> str | None:
    """CONSOLIDATED or STANDALONE when the covenant's own words say which figures it is tested on, else None."""
    if _GROUP.search(quote or ""):
        return "CONSOLIDATED"
    if _COMPANY.search(quote or ""):
        return "STANDALONE"
    return None
