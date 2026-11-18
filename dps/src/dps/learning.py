"""Phase 4 of "never miss silently": the learning loop.

What people decide in review is the best evidence of what DPS still does not understand. This module turns it
into three things:

1. **A weekly picture** of what DPS did not understand: possible covenants shown and how they were decided, AI
   disagreements, statements held by a check, lines not found, wordings remembered (``summary``).
2. **Suggestions** for the catalog: statement wordings reviewers confirmed (per borrower, phase 3) that the
   catalog still does not know, possible covenants a reviewer approved, and how often each covenant rule's
   proposals are rejected (``suggestions``).
3. **Learned wordings**: an administrator promotes a suggestion; from then on every new document uses it
   (``promote``, applied by the flows through ``apply``), and retiring it stops that at once. A wording that the
   catalog already reads as another line or covenant is refused, so learning cannot overrule what DPS knows.

Every decision can be exported as test cases (``test_cases``) for the golden set, so the regression gate keeps
what was learned.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from dps.models import Document, ExtractedTerm, LabelMemory, LearnedWording, utcnow

KINDS = ("STATEMENT_LINE", "COVENANT")
_IDENTITY = ("BALANCE_SHEET_DOES_NOT_BALANCE", "PART_ABOVE_TOTAL", "PROFIT_DOES_NOT_ADD_UP", "INCOME_DOES_NOT_ADD_UP",
             "CURRENT_ASSETS_TOO_SMALL")


class LearningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _week(when: datetime | date) -> str:
    d = when.date() if isinstance(when, datetime) else when
    return (d - timedelta(days=d.weekday())).isoformat()


def phrase_of(wording: str) -> str:
    """The wording without the amount and note number a reviewer's pick carries: "Borrowings - non-current 300.0"
    is "Borrowings - non-current"."""
    text = re.sub(r"\((?:refer\s+)?note[^)]*\)", " ", wording or "", flags=re.I)
    text = re.sub(r"^\s*(?:\(?[ivx]+\)|\(?[a-z]\)|\d+[.)])\s+", "", text, flags=re.I)
    return " ".join(re.sub(r"[\s\d.,()\-–]+$", "", text).split())


def _codes(issues_json: str | None) -> list[str]:
    try:
        return [str(i).split(":")[0] for i in json.loads(issues_json or "[]")]
    except ValueError:
        return []


def summary(db: Session, weeks: int = 8) -> dict:
    """Per week (Monday): what DPS read, and what it did not understand or a check caught."""
    since = utcnow() - timedelta(weeks=weeks)
    week = lambda: Counter()  # noqa: E731
    rows: dict[str, Counter] = defaultdict(week)
    for doc in db.scalars(select(Document).where(Document.uploaded_at >= since)):
        w = rows[_week(doc.uploaded_at)]
        if doc.type == "LOAN_AGREEMENT":
            w["agreements"] += 1
        elif doc.type == "FINANCIAL_STATEMENT":
            w["statements"] += 1
            review = json.loads(doc.review_json) if doc.review_json else {}
            codes = [str(i).split(":")[0] for i in review.get("issues", [])]
            w["secondReaderDiffers"] += codes.count("SECOND_READER_DIFFERS")
            w["accountingChecks"] += sum(1 for c in codes if c in _IDENTITY)
            w["unitChecks"] += codes.count("SCALE_JUMP")
            w["memoryConflicts"] += codes.count("LABEL_MEMORY_CONFLICT")
            w["missingLines"] += sum(1 for c in codes if c == "MISSING_LINE_ITEMS")
            w["likelyLines"] += len(review.get("likely", []))
            w["confirmedByBoth"] += len(review.get("confirmed", []))
            w["remembered"] += len(review.get("remembered", []))
    for t in db.scalars(select(ExtractedTerm).where(ExtractedTerm.created_at >= since)):
        w = rows[_week(t.created_at)]
        codes = _codes(t.issues_json)
        if t.category == "CANDIDATE":
            w["possibleCovenants"] += 1
            w["possibleApproved" if t.status == "PUBLISHED" else "possibleRejected" if t.status == "REJECTED"
              else "possibleOpen"] += 1
        w["aiReadDifferent"] += codes.count("AI_READ_DIFFERENT")
        w["aiOnly"] += codes.count("AI_ONLY")
        w["aiDidNotConfirm"] += codes.count("AI_DID_NOT_CONFIRM")
        if t.method == "REGEX+AI":
            w["covenantsConfirmedByBoth"] += 1
    for m in db.scalars(select(LabelMemory).where(LabelMemory.created_at >= since)):
        rows[_week(m.created_at)]["wordingsLearned"] += 1
    out = []
    for w in sorted(rows, reverse=True):
        c = rows[w]
        c["notUnderstood"] = (c["possibleCovenants"] + c["aiOnly"] + c["missingLines"] + c["memoryConflicts"])
        out.append({"week": w, **dict(c)})
    return {"weeks": out}


def suggestions(db: Session) -> dict:
    """What reviewers' decisions suggest adding to the catalog, with the evidence behind each."""
    from dps.extraction.mapping import base_metric
    from dps.extraction.statement import match_key

    lines: dict[tuple[str, str], dict] = {}
    for m in db.scalars(select(LabelMemory).order_by(LabelMemory.created_at)):
        phrase = phrase_of(m.source_label)
        if len(phrase) < 3 or match_key(phrase) == m.key:
            continue  # the catalog (or a learned wording) already reads it this way
        s = lines.setdefault((m.label, m.key), {"phrase": phrase, "key": m.key, "cps": set(),
                                                 "confirmedBy": set(), "lastConfirmed": None})
        s["cps"].add(m.cp_id)
        s["confirmedBy"].add(m.confirmed_by)
        s["lastConfirmed"] = m.created_at.isoformat()
    statement_lines = sorted(({**s, "cps": len(s["cps"]), "confirmedBy": sorted(s["confirmedBy"])}
                              for s in lines.values()), key=lambda s: (-s["cps"], s["phrase"]))
    covenants = [{"termId": t.id, "documentId": t.document_id, "name": t.name, "page": t.page, "quote": t.quote,
                  "approvedBy": t.reviewed_by}
                 for t in db.scalars(select(ExtractedTerm).where(ExtractedTerm.category == "CANDIDATE",
                                                                 ExtractedTerm.status == "PUBLISHED"))]
    decided: dict[str, Counter] = defaultdict(Counter)
    for t in db.scalars(select(ExtractedTerm).where(ExtractedTerm.category == "FINANCIAL",
                                                    ExtractedTerm.reviewed_by.is_not(None))):
        decided[base_metric(t.metric_raw)]["approved" if t.status == "PUBLISHED" else
                                           "rejected" if t.status == "REJECTED" else "edited"] += 1
    rules = sorted(({"metric": k, **dict(c), "precision": round(c["approved"] / max(1, c["approved"] + c["rejected"]),
                                                                  3)}
                    for k, c in decided.items()), key=lambda r: r["precision"])
    return {"statementLines": statement_lines, "covenants": covenants, "rules": rules}


def learned(db: Session, active_only: bool = False) -> list[dict]:
    stmt = select(LearnedWording).order_by(LearnedWording.added_at.desc())
    if active_only:
        stmt = stmt.where(LearnedWording.active == true())
    return [{"id": w.id, "kind": w.kind, "phrase": w.phrase, "key": w.key, "evidence": w.evidence, "active": w.active,
             "addedBy": w.added_by, "addedAt": w.added_at.isoformat(), "retiredBy": w.retired_by,
             "retiredAt": w.retired_at.isoformat() if w.retired_at else None} for w in db.scalars(stmt)]


def _check(kind: str, phrase: str, key: str) -> None:
    from dps.extraction.agreement import FINANCIAL_ALIASES, _best_metric
    from dps.extraction.statement import LINE_ALIASES, match_key

    if kind not in KINDS:
        raise LearningError("UNKNOWN_KIND", f"kind must be one of {', '.join(KINDS)}")
    if len(phrase.strip()) < 3:
        raise LearningError("TOO_SHORT", "The wording must be at least 3 characters")
    if kind == "STATEMENT_LINE":
        if key not in LINE_ALIASES:
            raise LearningError("UNKNOWN_KEY", f"{key} is not a statement line")
        now = match_key(phrase)
        if now is not None and now != key:
            raise LearningError("CONFLICT", f"DPS already reads '{phrase}' as {now}")
    else:
        if key not in FINANCIAL_ALIASES:
            raise LearningError("UNKNOWN_KEY", f"{key} is not a covenant metric")
        now = _best_metric(phrase)
        if now is not None and now != key:
            raise LearningError("CONFLICT", f"DPS already reads '{phrase}' as {now}")


def _gate(db: Session, kind: str, phrase: str, key: str) -> None:
    """Phase 4 benchmark gate: a wording is learned only if it does not undo what reviewers already decided.

    The bank's own decisions are the benchmark: a statement wording people confirmed as another line for any
    borrower, or a covenant proposal people rejected whose quote carries the wording, would be read wrongly again.
    """
    from dps.extraction.similarity import canonical

    words = phrase_of(phrase)
    if kind == "STATEMENT_LINE":
        other = db.scalar(select(LabelMemory.key).where(LabelMemory.label == canonical(words)[:300],
                                                        LabelMemory.key != key).limit(1))
        if other is not None:
            raise LearningError("WORSE_ON_DECISIONS", f"Reviewers confirmed '{phrase}' as {other} for a borrower; "
                                                      "learning it as " + key + " would read that wrongly again")
        return
    pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words.split()) + r"\b", re.I)
    rejected = sum(1 for q in db.scalars(select(ExtractedTerm.quote).where(ExtractedTerm.status == "REJECTED",
                                                                           ExtractedTerm.metric_raw == key))
                   if q and pattern.search(q))
    if rejected:
        raise LearningError("WORSE_ON_DECISIONS", f"Reviewers rejected {rejected} proposal(s) of {key} whose words "
                                                  f"contain '{phrase}'; learning it would propose them again")


def promote(db: Session, kind: str, phrase: str, key: str, user: str, evidence: int = 0) -> dict:
    """Add a wording to the catalog for every new document. Refused when DPS already reads it as something else."""
    from dps.services import audit

    phrase = " ".join(phrase.split())
    _check(kind, phrase, key)
    _gate(db, kind, phrase, key)
    row = db.scalar(select(LearnedWording).where(LearnedWording.kind == kind, LearnedWording.phrase == phrase))
    if row is not None and row.active:
        raise LearningError("ALREADY_LEARNED", f"'{phrase}' is already learned as {row.key}")
    if row is None:
        row = LearnedWording(kind=kind, phrase=phrase, key=key, evidence=evidence, active=True, added_by=user)
        db.add(row)
    else:
        row.key, row.evidence, row.active, row.added_by, row.added_at = key, evidence, True, user, utcnow()
        row.retired_by, row.retired_at = None, None
    db.flush()
    audit(db, user, "WORDING_LEARNED", "learned_wording", row.id, kind=kind, phrase=phrase, key=key, evidence=evidence)
    return next(w for w in learned(db) if w["id"] == row.id)


def retire(db: Session, wording_id: str, user: str) -> dict:
    from dps.services import audit

    row = db.get(LearnedWording, wording_id)
    if row is None:
        raise LearningError("NOT_FOUND", "No such learned wording")
    row.active, row.retired_by, row.retired_at = False, user, utcnow()
    db.flush()
    audit(db, user, "WORDING_RETIRED", "learned_wording", row.id, kind=row.kind, phrase=row.phrase, key=row.key)
    return next(w for w in learned(db) if w["id"] == row.id)


def apply(db: Session) -> None:
    """Hand the active learned wordings to the readers (called at the start of each document's flow)."""
    from dps.extraction.agreement import set_learned_covenants
    from dps.extraction.statement import set_learned_lines

    lines: dict[str, list[str]] = defaultdict(list)
    names: dict[str, list[str]] = defaultdict(list)
    for w in db.scalars(select(LearnedWording).where(LearnedWording.active == true())):
        (lines if w.kind == "STATEMENT_LINE" else names)[w.key].append(w.phrase)
    set_learned_lines(dict(lines))
    set_learned_covenants(dict(names))


def test_cases(db: Session) -> dict:
    """Reviewers' decisions as regression cases: what DPS must read this way, and what it must not propose."""
    lines = sorted({(phrase_of(m.source_label), m.key) for m in db.scalars(select(LabelMemory))})
    approved, rejected = [], []
    for t in db.scalars(select(ExtractedTerm).where(ExtractedTerm.reviewed_by.is_not(None),
                                                    ExtractedTerm.category.in_(("FINANCIAL", "CANDIDATE")))):
        case = {"quote": t.quote, "page": t.page, "metric": t.metric_raw, "operator": t.operator,
                "threshold": t.threshold, "documentId": t.document_id}
        if t.status == "PUBLISHED":
            approved.append(case)
        elif t.status == "REJECTED":
            rejected.append(case)
    return {"generatedAt": utcnow().isoformat(),
            "statementLines": [{"wording": w, "key": k} for w, k in lines],
            "covenantsApproved": approved, "covenantsRejected": rejected,
            "learnedWordings": learned(db, active_only=True)}
