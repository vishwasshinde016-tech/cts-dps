"""The language a document is written in, and its digits made plain (Phase 3b-2, 12 November 2026).

The rules read English; a document in another language is read by the AI reader, which returns the English name of
each covenant or line with the words copied in the original language, checked against the page as for English. To
check a number the page's digits are first made plain: Arabic-Indic, Persian, Devanagari, Bengali, Tamil, Sinhala
(Lith) and Thai digits become 0-9, and the Arabic decimal and thousands marks become "." and ",".
"""

from __future__ import annotations

import re

# Non-Latin scripts by Unicode block, and the language a bank document in that script is most likely in.
_SCRIPTS = [
    ("ar", re.compile(r"[؀-ۿݐ-ݿ]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
    ("bn", re.compile(r"[ঀ-৿]")),
    ("ta", re.compile(r"[஀-௿]")),
    ("si", re.compile(r"[඀-෿]")),
    ("th", re.compile(r"[฀-๿]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("zh", re.compile(r"[一-鿿]")),
    ("ja", re.compile(r"[぀-ヿ]")),
    ("ko", re.compile(r"[가-힯]")),
]
# Common words of Latin-script languages (a document's most frequent short words tell them apart).
_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "shall", "is", "for", "be", "by", "any", "or", "with", "on", "as"},
    "fr": {"le", "la", "les", "de", "des", "et", "du", "en", "un", "une", "pour", "par", "est", "sur", "au"},
    "es": {"el", "la", "los", "las", "de", "del", "y", "en", "por", "para", "con", "una", "que", "se", "al"},
    "pt": {"o", "a", "os", "as", "de", "do", "da", "e", "em", "para", "com", "uma", "que", "pelo", "dos"},
    "de": {"der", "die", "das", "und", "zu", "den", "von", "mit", "ist", "des", "im", "auf", "eine", "für", "nicht"},
    "id": {"dan", "yang", "di", "untuk", "dengan", "dari", "pada", "dalam", "atau", "ini", "akan", "oleh"},
    "it": {"il", "la", "di", "e", "che", "per", "del", "della", "un", "una", "con", "non", "sono", "dei"},
    "nl": {"de", "het", "en", "van", "een", "in", "is", "op", "te", "voor", "met", "zijn", "niet", "aan"},
}
NAMES = {"en": "English", "fr": "French", "es": "Spanish", "pt": "Portuguese", "de": "German", "id": "Indonesian",
         "it": "Italian", "nl": "Dutch", "ar": "Arabic", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil",
         "si": "Sinhala", "th": "Thai", "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean"}

_DIGITS = {}
for zero in (0x0660, 0x06F0, 0x0966, 0x09E6, 0x0BE6, 0x0DE6, 0x0E50):  # Arabic-Indic .. Thai
    for i in range(10):
        _DIGITS[chr(zero + i)] = str(i)
_DIGITS["٫"] = "."  # Arabic decimal separator
_DIGITS["٬"] = ","  # Arabic thousands separator
_PLAIN = str.maketrans(_DIGITS)


def plain_digits(text: str) -> str:
    """Every digit as 0-9 and the Arabic decimal and thousands marks as "." and ","."""
    return (text or "").translate(_PLAIN)


def detect(text: str) -> str:
    """The language of a document as an ISO 639-1 code; "en" when nothing says otherwise."""
    sample = (text or "")[:20000]
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return "en"
    for code, pattern in _SCRIPTS:
        if len(pattern.findall(sample)) > 0.2 * len(letters):
            return code
    words = re.findall(r"[a-zà-öø-ÿ]+", sample.lower())
    if not words:
        return "en"
    scores = {code: sum(1 for w in words if w in stop) for code, stop in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    # English unless another language's common words clearly outnumber it (a French clause quoted in an English
    # agreement does not make it French).
    return best if best != "en" and scores[best] > 1.5 * scores["en"] and scores[best] >= 10 else "en"


def name(code: str) -> str:
    return NAMES.get(code, code)
