"""The covenant taxonomy: one versioned list of covenant types that names every covenant DPS extracts.

``default_taxonomy.yaml`` holds the types (code, category, name, description, aliases, keywords, defaults);
``dps_overlay.yaml`` adds the types DPS also reads and maps the rules' own metric names (``dps_keys``) to codes.
A bank can add overlays without code: a YAML file, or a folder of them, in ``DPS_COVENANT_TAXONOMY_PATH``.

Overlay rules: an entry whose ``code`` exists patches it (aliases, keywords and dps_keys are added to the existing
ones unless ``replace_aliases: true``); ``enabled: false`` disables a type; a new code adds a type. Every overlay's
``version`` is appended to the taxonomy version, which is stamped on every result.

The file is checked when it is loaded: an unknown category, a bad operator or unit, an alias given to two types of
the same category, or a DPS key mapped to two codes stops the worker instead of naming covenants wrongly. An alias
two categories share (``Change of Control`` is a negative covenant and an event of default) goes to the covenant
category; such cases are listed in ``notes``.

Matching is careful with loose names: aliases of one or two letters ("CR", "DP") are never matched; a short
upper-case acronym ("DSCR", "TOL", "QIS") matches only as a whole upper-case word; a one-word alias ("Insurance",
"Purpose") matches only a name that is exactly that word; longer aliases match inside a name, the longest first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FILES = (HERE / "default_taxonomy.yaml", HERE / "dps_overlay.yaml")
ENV = "DPS_COVENANT_TAXONOMY_PATH"

# When two categories share an alias, the first here keeps it.
CATEGORY_PRIORITY = ("financial", "negative", "affirmative", "reporting", "event_of_default")
COVENANT_CATEGORIES = ("financial", "affirmative", "negative", "reporting")
OPERATORS = {">=", ">", "<=", "<", "="}
UNITS = {"ratio", "multiple", "percent", "amount", "days", "count"}
LIST_FIELDS = ("aliases", "keywords", "dps_keys")


class TaxonomyError(ValueError):
    """The taxonomy files are not valid; nothing is loaded."""


@dataclass(frozen=True)
class CovenantType:
    code: str
    category: str
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    quantitative: bool = False
    default_operator: str | None = None
    unit: str | None = None
    frequency: str | None = None
    attributes: tuple[dict, ...] = ()
    dps_keys: tuple[str, ...] = ()

    @property
    def is_other(self) -> bool:
        return self.code.endswith("_OTHER") or self.code.endswith("_OTHER_INFO")


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(text or "").upper()).strip()


def _token(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").upper()).strip("_")


@dataclass
class Taxonomy:
    version: str
    categories: dict[str, str]
    types: dict[str, CovenantType]
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_dps_key: dict[str, str] = {}
        for t in self.types.values():
            for key in t.dps_keys:
                self.by_dps_key[key] = t.code
        ordered = sorted(self.types.values(), key=lambda t: (_rank(t.category), t.code))
        self._exact: dict[str, str] = {}
        self._phrases: list[tuple[str, str]] = []
        self._acronyms: list[tuple[re.Pattern[str], str]] = []
        for t in ordered:
            for alias in (t.name, *t.aliases):
                n = _norm(alias)
                if len(n.replace(" ", "")) <= 2:
                    continue
                self._exact.setdefault(n, t.code)
                if " " in n:
                    self._phrases.append((n, t.code))
                elif n == alias.strip() and n.isupper() and len(n) <= 6:
                    self._acronyms.append((re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])"), t.code))
        self._phrases.sort(key=lambda p: -len(p[0]))

    # ---------------------------------------------------------------- lookups
    def get(self, code: str | None) -> CovenantType | None:
        return self.types.get(str(code or ""))

    def code_for(self, text: str | None) -> str | None:
        """The type a name, code or DPS metric key means; None when none fits."""
        if not text:
            return None
        token = _token(text)
        if token in self.types:
            return token
        if token in self.by_dps_key:
            return self.by_dps_key[token]
        n = _norm(text)
        if n in self._exact:
            return self._exact[n]
        spaced = f" {n} "
        for phrase, code in self._phrases:
            if f" {phrase} " in spaced:
                return code
        for pattern, code in self._acronyms:
            if pattern.search(str(text)):
                return code
        return None

    def more_specific(self, text: str | None, code: str | None) -> str | None:
        """A type of the same category named in ``text`` by a longer phrase that contains one of ``code``'s phrases
        ("first lien leverage ratio" beside "leverage ratio"): the clause's own narrower test, which must not be read
        as the broader one. None when there is none."""
        if not text or not code:
            return None
        spaced = f" {_norm(text)} "
        own = [p for p, c in self._phrases if c == code and f" {p} " in spaced]
        if not own:
            return None
        category = self.category_of(code)
        for phrase, other in self._phrases:  # longest first
            if other == code or f" {phrase} " not in spaced or self.category_of(other) != category:
                continue
            if any(len(phrase) > len(o) and f" {o} " in f" {phrase} " for o in own):
                return other
        return None

    def dps_key(self, code: str | None) -> str | None:
        """The rules' own metric name for a code (the first of its dps_keys), when it has one."""
        t = self.get(code)
        return t.dps_keys[0] if t and t.dps_keys else None

    def category_of(self, code: str | None) -> str | None:
        t = self.get(code)
        return t.category if t else None

    def of_categories(self, categories) -> list[CovenantType]:
        wanted = set(categories)
        return [t for t in self.types.values() if t.category in wanted]

    def prompt_lines(self, categories) -> str:
        """One line per type for the agent: code, name and what it means."""
        lines = []
        for cat in categories:
            types = [t for t in self.of_categories([cat]) if not t.is_other] + \
                    [t for t in self.of_categories([cat]) if t.is_other]
            if not types:
                continue
            lines.append(f"{self.categories.get(cat, cat)}:")
            lines += [f"- {t.code}: {t.name}. {t.description}".rstrip() for t in types]
        return "\n".join(lines)

    def words_for(self, categories) -> re.Pattern[str] | None:
        """A pattern for pages likely to hold these categories: their multi-word aliases and keywords."""
        words = set()
        for t in self.of_categories(categories):
            for w in (*t.aliases, *t.keywords):
                n = _norm(w)
                if " " in n or len(n) >= 6:
                    words.add(re.escape(n.lower()).replace(r"\ ", r"\s+"))
        if not words:
            return None
        return re.compile(r"\b(?:" + "|".join(sorted(words, key=len, reverse=True)) + r")\b", re.I)


def _rank(category: str) -> int:
    return CATEGORY_PRIORITY.index(category) if category in CATEGORY_PRIORITY else len(CATEGORY_PRIORITY)


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TaxonomyError(f"{path.name}: not a YAML mapping")
    return data


def _extra_files() -> list[Path]:
    where = os.environ.get(ENV, "").strip()
    if not where:
        return []
    p = Path(where)
    if p.is_dir():
        return sorted(x for x in p.iterdir() if x.suffix.lower() in (".yaml", ".yml"))
    if p.is_file():
        return [p]
    raise TaxonomyError(f"{ENV}={where} is neither a file nor a folder")


def _merge_documents(documents: list[tuple[str, dict]]) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    versions, categories, raw = [], {}, {}
    for name, doc in documents:
        if doc.get("version"):
            versions.append(str(doc["version"]))
        for c in doc.get("categories") or []:
            categories[str(c["code"])] = str(c.get("name") or c["code"])
        for entry in doc.get("types") or []:
            if not isinstance(entry, dict) or not entry.get("code"):
                raise TaxonomyError(f"{name}: a type without a code")
            code = str(entry["code"])
            if code in raw:
                merged = dict(raw[code])
                for k, v in entry.items():
                    if k in LIST_FIELDS and not (k == "aliases" and entry.get("replace_aliases")):
                        merged[k] = list(dict.fromkeys([*(merged.get(k) or []), *(v or [])]))
                    elif k != "replace_aliases":
                        merged[k] = v
                raw[code] = merged
            else:
                raw[code] = {k: v for k, v in entry.items() if k != "replace_aliases"}
    return versions, categories, raw


def build(documents: list[tuple[str, dict]]) -> Taxonomy:
    """Merges the taxonomy documents in order and checks the result."""
    versions, categories, raw = _merge_documents(documents)
    types: dict[str, CovenantType] = {}
    problems, notes = [], []
    for code, e in raw.items():
        if e.get("enabled") is False:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
            problems.append(f"{code}: a code is UPPER_SNAKE")
        cat = e.get("category")
        if cat not in categories:
            problems.append(f"{code}: unknown category {cat!r}")
            continue
        if e.get("default_operator") not in (None, *OPERATORS):
            problems.append(f"{code}: default_operator {e.get('default_operator')!r}")
        if e.get("unit") not in (None, *UNITS):
            problems.append(f"{code}: unit {e.get('unit')!r}")
        types[code] = CovenantType(
            code, cat, str(e.get("name") or code), str(e.get("description") or "").strip(),
            tuple(str(a) for a in e.get("aliases") or []), tuple(str(k) for k in e.get("keywords") or []),
            bool(e.get("quantitative", False)), e.get("default_operator"), e.get("unit"), e.get("frequency"),
            tuple(e.get("attributes") or []), tuple(str(k) for k in e.get("dps_keys") or []))
    owner: dict[tuple[str, str], str] = {}
    for t in types.values():
        for alias in t.aliases:
            n = _norm(alias)
            other = owner.get((t.category, n))
            if other and other != t.code:
                problems.append(f"alias {alias!r} is on {other} and {t.code} (same category)")
            owner[(t.category, n)] = t.code
    seen: dict[str, str] = {}
    for t in types.values():
        for key in t.dps_keys:
            if key in seen and seen[key] != t.code:
                problems.append(f"DPS key {key} is mapped to {seen[key]} and {t.code}")
            seen[key] = t.code
    across: dict[str, set[str]] = {}
    for (cat, n), code in owner.items():
        across.setdefault(n, set()).add(f"{cat}:{code}")
    notes += [f"alias {n!r} shared by {sorted(v)}; the covenant category keeps it" for n, v in across.items()
              if len(v) > 1]
    if problems:
        raise TaxonomyError("; ".join(problems[:20]))
    return Taxonomy("+".join(versions) or "unversioned", categories, types, notes)


@cache
def get() -> Taxonomy:
    """The taxonomy in use: the default, the DPS overlay, then any overlay in DPS_COVENANT_TAXONOMY_PATH."""
    files = [*FILES, *_extra_files()]
    return build([(p.name, _read(p)) for p in files])
