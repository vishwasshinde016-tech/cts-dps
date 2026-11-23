"""The statement's period end, read from the document itself (CTS integration, 23 Nov 2026: one upload for every
document type, so the bank app's user does not type the period date; the reviewer confirms it with the figures).

Only a date the heading ties to the period counts: "as at 31 March 2024", "for the year ended 31st March, 2024",
"quarter ended 30.06.2024", "period ending March 31, 2024". A date anywhere else (a signature date, a board meeting)
is never taken. When the heading gives several period ends (this year and the comparative year), the latest is the
statement's period. Day-first is the default for numeric dates (Indian and most bank documents); a numeric date that
is only valid month-first ("03/31/2024") is read month-first.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9

_MONTH = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d\d)"
_LEAD = r"(?:as\s+(?:at|on|of)|(?:year|period|quarter|half[\s-]*year|months?)\s+(?:ended|ending)|ended|ending)\s*:?\s*"
_DATES = (
    _DAY + r"[\s\-]+" + _MONTH + r",?[\s\-]+" + _YEAR,                  # 31 March 2024, 31st Mar, 2024, 31-Mar-2024
    _MONTH + r"\s+" + _DAY + r",?\s+" + _YEAR,                          # March 31, 2024
    r"(?P<a>[0-3]?\d)[./\-](?P<b>[0-3]?\d)[./\-]" + _YEAR,              # 31.03.2024, 31/03/2024, 03/31/2024
    _YEAR + r"-(?P<m2>[01]\d)-(?P<d2>[0-3]\d)",                          # 2024-03-31
)
PATTERNS = [re.compile(_LEAD + d, re.IGNORECASE) for d in _DATES]


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _date(m: re.Match) -> date | None:
    g = m.groupdict()
    year = int(g["year"])
    if g.get("mon"):
        month = MONTHS.get(g["mon"].lower().rstrip("."))
        return _valid(year, month, int(g["day"])) if month else None
    if g.get("m2"):
        return _valid(year, int(g["m2"]), int(g["d2"]))
    a, b = int(g["a"]), int(g["b"])
    return _valid(year, b, a) or _valid(year, a, b)  # day-first, else month-first when only that is a date


def period_end(*texts: str) -> date | None:
    """The latest period end the heading names, or None when it names none."""
    found = []
    for text in texts:
        for pattern in PATTERNS:
            for m in pattern.finditer(text or ""):
                d = _date(m)
                if d is not None and 1990 <= d.year <= 2100:
                    found.append(d)
    return max(found) if found else None
