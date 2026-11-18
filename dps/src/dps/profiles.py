"""Country profiles and what applies to one borrower (Phase 2, 11 November 2026).

Core keeps a profile per country the bank lends in (Settings > Countries) and each borrower's own settings over it.
``GET /api/v1/credit-proposals/{cpId}/profile`` gives what applies to one borrower: its currency (and whether it is
known), the month its financial year ends, whether group or company figures are read and the number style its
documents use. Adding a country is a profile in Core; DPS reads it, with no code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_STYLES = ("INDIAN", "WESTERN", "EUROPEAN")
_ENTITIES = ("STANDALONE", "CONSOLIDATED")


@dataclass(frozen=True)
class LoanProfile:
    currency: str
    currency_known: bool = True
    fiscal_year_end_month: int = 3
    statement_entity: str = "STANDALONE"
    number_style: str = "INDIAN"
    country: str | None = None
    currency_words: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_core(cls, body: dict) -> LoanProfile | None:
        """Core's profile answer, or None when it is not one (an older Core, a broken answer)."""
        currency = body.get("currency")
        month = body.get("fiscalYearEndMonth")
        if not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)):
            return None
        return cls(currency=currency, currency_known=body.get("currencyKnown") is not False,
                   fiscal_year_end_month=month if isinstance(month, int) and 1 <= month <= 12 else 3,
                   statement_entity=body.get("statementEntity") if body.get("statementEntity") in _ENTITIES
                   else "STANDALONE",
                   number_style=body.get("numberStyle") if body.get("numberStyle") in _STYLES else "WESTERN",
                   country=body.get("country"),
                   currency_words=tuple(w for w in body.get("currencyWords") or () if isinstance(w, str) and w))


def currency_words(profiles: list[dict]) -> list[tuple[str, re.Pattern]]:
    """The currency words of every country profile ("KShs" is KES), as patterns for ``money.detect_currency``.

    A word two countries use for different currencies ("Rs." in India, Sri Lanka, Pakistan and Nepal) names no
    currency and is left out: the loan's currency is used for it, as before.
    """
    owners: dict[str, set[str]] = {}
    for p in profiles:
        code = p.get("currency")
        if not (isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code)):
            continue
        for word in p.get("currencyWords") or ():
            if isinstance(word, str) and word.strip():
                owners.setdefault(word.strip().lower(), set()).add(code)
    out = []
    for word, codes in owners.items():
        if len(codes) == 1:
            out.append((next(iter(codes)), _word_pattern(word)))
    return out


def _word_pattern(word: str) -> re.Pattern:
    start = r"\b" if word[0].isalnum() else ""
    end = r"\b" if word[-1].isalnum() else ""
    return re.compile(start + r"\s+".join(re.escape(w) for w in word.split()) + end, re.I)
