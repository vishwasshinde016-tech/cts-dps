"""Candidate ledger (19 Nov 2026): every covenant-shaped sentence of the whole document ends with an outcome.

Before this, the coverage check looked only at the pages sent to the Document Agent, so a covenant outside the
"Covenants" section could be missed with no gap reported. Now every sentence of every page that could set a duty is a
candidate, and each one ends in exactly one state:

- ``FINDING``: a field quotes it (same page or a neighbour);
- ``COVERED``: a covenant of the same kind was extracted on that page or a neighbour (one insurance covenant covers
  the four "shall" sentences of its clause);
- ``NOT_A_COVENANT``: the Document Agent's residual read said why it is not a covenant (``omissions.py``);
- ``UNRESOLVED``: none of the above. These become review items, so nothing can be missed silently.

Candidates come from two routes:
- ``DUTY``: an obligation with a comparison, a financial word or a covenant subject (``pages.is_duty``);
- ``CUE``: a sentence that binds the borrower and names a covenant kind from the taxonomy, for duties worded outside
  the usual patterns.

Each candidate also keeps:
- its **qualifiers** ("subject to", "unless", "provided that", "except", "only if", "so long as" ...), from the
  sentence itself and from a next sentence that starts with one;
- its **references** ("Clause 21.3", "Schedule 5"), each resolved to the page whose heading starts with it, or left
  unresolved.

A covenant whose sentence carries a qualifier or an unresolved reference goes to a person with that text attached,
because a condition ("FCCR at least 1.10x when Excess Availability is below USD 10 million") changes what the
covenant means.
"""

from __future__ import annotations

import re

from dps.skills.common import issue
from dps.skills.covenants import coverage
from dps.skills.covenants.pages import BORROWER_DUTY, CONDITION_PRECEDENT, NOT_A_DUTY, is_duty, sentences

FINDING, COVERED, NOT_A_COVENANT, UNRESOLVED = "FINDING", "COVERED", "NOT_A_COVENANT", "UNRESOLVED"
STATES = (FINDING, COVERED, NOT_A_COVENANT, UNRESOLVED)
MAX_TEXT = 300

QUALIFIER = re.compile(
    r"\b(?:subject\s+to|unless|except(?:\s+(?:for|as|that|with|to\s+the\s+extent))?|provided\s+(?:that|always|however)|"
    r"other\s+than|save\s+(?:as|for)|without\s+prejudice\s+to|to\s+the\s+extent\s+(?:that|permitted)|"
    r"only\s+if|if\s+and\s+only\s+if|(?:for\s+)?so\s+long\s+as|notwithstanding|in\s+the\s+event\s+(?:that|of)|"
    r"at\s+any\s+time\s+(?:when|that|during\s+which)|when(?:ever)?\s+(?:the\s+)?[\w\s]{2,40}?\s+(?:is|are|falls|"
    r"exceeds?|drops?)\s+(?:below|above|less|more|greater|lower)|"
    r"without\s+(?:the\s+)?(?:prior\s+)?(?:written\s+)?(?:consent|approval))\b", re.I)
# A reference to a part of the agreement: "Clause 21.3", "clause 21.3(b)", "Schedule 5", "Annexure II", "Section 7.01".
REFERENCE = re.compile(
    r"\b(clause|section|article|schedule|annex(?:ure)?|appendix|exhibit|paragraph)\s+"
    r"((?:\d+(?:\.\d+)*)(?:\s*\([a-z0-9]{1,4}\))*|[IVXLC]{1,6}\b|[A-Z]\b)", re.I)
# Self references that need no lookup ("this Clause 21", "Clause 1.1 (Definitions)") stay listed, resolved or not.


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def qualifiers(sentence: str, following: str | None = None) -> list[str]:
    """The qualifying words of a duty: the clause part from each qualifier in the sentence, and a next sentence that
    starts with one ("Provided that ...")."""
    out = []
    for m in QUALIFIER.finditer(sentence):
        out.append(_flat(sentence[m.start():m.start() + 160]))
    if following and QUALIFIER.match(following.strip()):
        out.append(_flat(following[:200]))
    seen, kept = set(), []
    for q in out:
        key = q.lower()[:60]
        if key not in seen:
            seen.add(key)
            kept.append(q)
    return kept[:4]


def _kind(word: str) -> str:
    w = word.lower()
    if w.startswith("annex"):
        return "annex"
    return {"section": "clause", "article": "clause", "paragraph": "clause"}.get(w, w)


def headings(pages: list[dict]) -> dict[tuple[str, str], int]:
    """(kind, number) -> the first page with a line that starts with that schedule or clause number."""
    found: dict[tuple[str, str], int] = {}
    named = re.compile(r"(?im)^[ \t]*(schedule|annex(?:ure)?|appendix|exhibit|article|section|clause)[ \t]+"
                       r"(\d+(?:\.\d+)*|[IVXLC]{1,6}\b|[A-Z]\b)")
    numbered = re.compile(r"(?m)^[ \t]*(\d{1,3}(?:\.\d{1,3}){0,3})[.)]?[ \t]+[A-Z(]")
    for p in pages:
        text = p.get("text") or ""
        for m in named.finditer(text):
            found.setdefault((_kind(m.group(1)), m.group(2).upper()), int(p["no"]))
        for m in numbered.finditer(text):
            found.setdefault(("clause", m.group(1)), int(p["no"]))
    return found


def references(sentence: str, index: dict[tuple[str, str], int]) -> list[dict]:
    """The parts of the agreement a sentence refers to (``index`` from ``headings``), each with the page it starts
    on (None: not found)."""
    out, seen = [], set()
    for m in REFERENCE.finditer(sentence):
        kind, number = _kind(m.group(1)), m.group(2)
        base = re.sub(r"\s*\(.*$", "", number).upper()
        ref = f"{m.group(1).title()} {number}"
        if ref.lower() in seen:
            continue
        seen.add(ref.lower())
        page = index.get((kind, base))
        if page is None and kind == "clause":  # "Clause 21.3" when only "21." heads the clause
            page = index.get(("clause", base.split(".")[0]))
        out.append({"ref": ref, "page": page})
    return out[:6]


def candidates(pages: list[dict]) -> list[dict]:
    """Every sentence of the document that could set a covenant, with its route, kind, qualifiers and references."""
    out = []
    index = headings(pages)
    for p in pages:
        no = int(p["no"])
        sents = sentences(p.get("text") or "")
        for i, s in enumerate(sents):
            if len(s) < 25 or coverage.MECHANICS.search(s) or coverage.DEFINES.search(s):
                continue
            if is_duty(s):
                route = "DUTY"
            elif (BORROWER_DUTY.search(s) and not CONDITION_PRECEDENT.search(s) and not NOT_A_DUTY.search(s)
                  and coverage.sentence_kind(s)):
                route = "CUE"
            else:
                continue
            following = sents[i + 1] if i + 1 < len(sents) else None
            out.append({"id": f"p{no}s{i}", "page": no, "text": s[:MAX_TEXT], "route": route,
                        "kind": coverage.sentence_kind(s), "qualifiers": qualifiers(s, following),
                        "references": references(s, index), "state": UNRESOLVED, "reason": None,
                        "fieldId": None, "_full": s, "_before": sents[i - 1] if i else "", "_after": following or ""})
    return out


def settle(entries: list[dict], fields: list[dict], decisions: dict[str, dict] | None = None) -> list[dict]:
    """Gives each candidate its state against the fields (and the residual read's decisions), in place."""
    quotes: dict[int, list[tuple[str, dict]]] = {}
    kinds: dict[int, dict[str, dict]] = {}
    for f in fields:
        src = f.get("source") or {}
        if src.get("page") is None:
            continue
        no = int(src["page"])
        if src.get("quote"):
            quotes.setdefault(no, []).append((str(src["quote"]), f))
        kind = coverage.field_kind(f)
        if kind:
            kinds.setdefault(no, {}).setdefault(kind, f)
    for e in entries:
        no = e["page"]
        near = quotes.get(no, []) + quotes.get(no - 1, []) + quotes.get(no + 1, [])
        hit = next((f for q, f in near if coverage._explains(q, e["_full"])), None)
        if hit is not None:
            e["state"], e["fieldId"], e["reason"] = FINDING, hit.get("id"), None
            continue
        kind_hit = None
        if e["kind"]:
            for n in (no, no - 1, no + 1):
                kind_hit = kinds.get(n, {}).get(e["kind"])
                if kind_hit is not None:
                    break
        if kind_hit is not None:
            e["state"], e["fieldId"], e["reason"] = COVERED, kind_hit.get("id"), None
            continue
        d = (decisions or {}).get(e["id"])
        if d and d.get("decision") == "NOT_A_COVENANT":
            e["state"], e["reason"] = NOT_A_COVENANT, (d.get("reason") or "")[:200] or "not a covenant"
            continue
        e["state"] = UNRESOLVED
        if d and d.get("decision") == "UNSURE":
            e["reason"] = ("the Document Agent was unsure: " + (d.get("reason") or ""))[:200]
    return entries


def attach(entries: list[dict], fields: list[dict]) -> None:
    """Puts each found candidate's qualifiers and references on its field; a qualified covenant, or one that points
    to a part of the agreement not found, goes to a person with that text."""
    by_id = {f.get("id"): f for f in fields}
    for e in entries:
        f = by_id.get(e["fieldId"]) if e["state"] == FINDING else None
        if f is None or f.get("group") not in coverage.COVENANT_GROUPS:
            continue
        value = f.setdefault("value", {})
        codes = {i.get("code") for i in f.get("issues") or []}
        if e["qualifiers"]:
            have = value.setdefault("qualifiers", [])
            for q in e["qualifiers"]:
                if q not in have:
                    have.append(q)
            if "QUALIFIED" not in codes:
                f.setdefault("issues", []).append(issue("QUALIFIED", "the covenant has a condition or exception: "
                                                                     + "; ".join(e["qualifiers"])[:240]))
                f["status"] = "NEEDS_REVIEW"
        if e["references"]:
            have = value.setdefault("references", [])
            for r in e["references"]:
                if r not in have:
                    have.append(r)
            missing = [r["ref"] for r in e["references"] if r["page"] is None]
            if missing and "UNRESOLVED_REFERENCE" not in codes:
                f.setdefault("issues", []).append(issue("UNRESOLVED_REFERENCE", "refers to " + ", ".join(missing)
                                                        + ", which was not found in this document"))
                f["status"] = "NEEDS_REVIEW"


def unresolved(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e["state"] == UNRESOLVED]


def review_items(entries: list[dict], pages: list[dict], pack_name: str) -> list[dict]:
    """Every candidate still unresolved, as review fields: one per page and kind (one clause area), quoting the first
    sentence and listing the others, so a person confirms or dismisses each. Nothing is missed silently."""
    from dps.skills.common import make_field

    groups: dict[tuple[int, str | None], list[dict]] = {}
    for e in unresolved(entries):
        groups.setdefault((e["page"], e["kind"]), []).append(e)
    out = []
    for (page, kind), items in sorted(groups.items(), key=lambda g: (g[0][0], g[0][1] or "")):
        first = items[0]
        value = {"metric": kind or "OTHER", "operator": None, "threshold": None, "unit": None, "expression": None,
                 "candidates": [e["id"] for e in items],
                 "sentences": [e["text"] for e in items[:8]]}
        if any(e["qualifiers"] for e in items):
            value["qualifiers"] = [q for e in items for q in e["qualifiers"]][:6]
        f = make_field(pack_name, pages, "POSSIBLE_COVENANT", f"Unresolved duty on page {page}", value, page,
                       first["_full"], method="COVERAGE", confidence=0.3, key=f"ledger-{first['id']}")
        f["status"] = "NEEDS_REVIEW"
        why = next((e["reason"] for e in items if e["reason"]), None)
        f["issues"].append(issue("UNRESOLVED_CANDIDATE", f"{len(items)} duty sentence(s) on page {page} that no "
                                                         "extracted covenant explains" + (f" ({why})" if why else "")
                                 + "; confirm or dismiss"))
        for e in items:
            e["fieldId"] = f["id"]
        out.append(f)
    return out


def summary(entries: list[dict], pages: list[dict]) -> dict:
    """The ledger for the result: counts per state, the share explained, and every entry (text cut short)."""
    counts = {s: 0 for s in STATES}
    for e in entries:
        counts[e["state"]] += 1
    total = len(entries)
    explained = counts[FINDING] + counts[COVERED] + counts[NOT_A_COVENANT]
    return {
        "scope": "WHOLE_DOCUMENT", "pages": len(pages), "candidates": total, "accounted": total,
        "finding": counts[FINDING], "covered": counts[COVERED], "notCovenant": counts[NOT_A_COVENANT],
        "unresolved": counts[UNRESOLVED],
        "percentExplained": round(100.0 * explained / total, 1) if total else 100.0,
        "entries": [{k: e[k] for k in ("id", "page", "route", "kind", "state", "reason", "fieldId", "text",
                                       "qualifiers", "references")} for e in entries],
    }
