"""Business logic behind the API (no FastAPI types here)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from dps.models import (
    AuditEvent,
    Document,
    DocumentCpLink,
    EventOutbox,
    ExtractedTerm,
    LabelMemory,
    StatementValue,
    utcnow,
)
from dps.money import currency_of


def audit(db: Session, actor: str, action: str, target_type: str, target_id: str, **detail) -> None:
    db.add(AuditEvent(actor=actor, action=action, target_type=target_type, target_id=target_id,
                      detail_json=json.dumps(detail, default=str)))


def values_series(db: Session, entity: str, keys: list[str], to: date, frm: date | None) -> dict:
    """C2: Core's REST data-source contract.

    Point-in-time (``frm`` is None): latest value per key with as_of_date <= to.
    Windowed: every value with frm <= as_of_date <= to. Superseded values are never served.
    """
    base = select(StatementValue).where(
        StatementValue.cp_id == entity,
        StatementValue.key.in_(keys),
        StatementValue.as_of_date <= to,
        StatementValue.superseded == false(),
        StatementValue.source_type != "COMPARATIVE",  # inputs to the 12-month figures, not a period's own figures
    )
    if frm is not None:
        base = base.where(StatementValue.as_of_date >= frm)
    rows = list(db.scalars(base.order_by(StatementValue.as_of_date, StatementValue.created_at)))
    if frm is None:
        latest: dict[str, StatementValue] = {}
        for r in rows:
            latest[r.key] = r  # ordered ascending, so the last one wins
        rows = list(latest.values())
    # Money for any country: amounts are served in the CP's current money unit only (that of its newest figure). An
    # older period kept in another unit is left out, so a ratio never divides LKR million by INR crore; the covenant
    # reads "could not be checked" until the figure is re-entered.
    money = [r for r in rows if currency_of(r.unit)]
    if money:
        current = max(money, key=lambda r: (r.as_of_date, r.created_at)).unit
        rows = [r for r in rows if not currency_of(r.unit) or r.unit == current]
    by_date: dict[date, dict] = defaultdict(dict)
    for r in rows:
        by_date[r.as_of_date][r.key] = r.value
    points = [{"asOfDate": d.isoformat(), "values": v} for d, v in sorted(by_date.items())]
    present = {r.key for r in rows}
    changed = db.scalar(select(func.max(StatementValue.created_at)).where(StatementValue.cp_id == entity))
    return {
        "cpId": entity,
        "points": points,
        "missingKeys": [k for k in keys if k not in present],
        "sourceChangedAt": changed.isoformat() if changed else None,
    }


def previous_values(db: Session, cp_ids: list[str], before: date | None, keys: tuple[str, ...]
                    ) -> dict[str, tuple[float, date]]:
    """The latest value of each key for these CPs, for a period before ``before`` (current values only)."""
    if not cp_ids or before is None:
        return {}
    out: dict[str, tuple[float, date]] = {}
    rows = db.scalars(select(StatementValue).where(
        StatementValue.cp_id.in_(cp_ids), StatementValue.key.in_(keys), StatementValue.as_of_date < before,
        StatementValue.superseded == false(), StatementValue.source_type != "COMPARATIVE")
        .order_by(StatementValue.as_of_date.desc()))
    for r in rows:
        out.setdefault(r.key, (r.value, r.as_of_date))
    return out


def statement_money(db: Session, cp: str, as_of: date | None) -> str:
    """The money unit this CP's figures for the period are kept in (money for any country): the unit of a figure
    already stored for it, else the default currency's (settings: DPS_DEFAULT_CURRENCY)."""
    from dps.money import money_unit
    from dps.settings import get_settings

    row = db.scalar(select(StatementValue.unit).where(
        StatementValue.cp_id == cp, StatementValue.as_of_date == as_of, StatementValue.superseded == false(),
        StatementValue.source_type != "COMPARATIVE", StatementValue.unit.like("%\\_%", escape="\\")).limit(1))
    return row if currency_of(row) else money_unit(get_settings().default_currency)


def _covenant_key(term: ExtractedTerm) -> str:
    """Which covenant a term is about: its library covenant, or its own name when it is a custom one."""
    return term.covenant_master_id or term.metric_raw.lower()


_UNIT_TEXT = {"PERCENT": "%", "%": "%", "INR_CR": " INR crore", "USD": " USD", "LKR": " LKR", "EUR": " EUR",
              "GBP": " GBP", "SGD": " SGD", "AED": " AED"}
_OPERATOR_TEXT = {">=": "at least", ">": "more than", "<=": "at most", "<": "less than"}


def _manual_rule(term: ExtractedTerm) -> str | None:
    """A covenant a person assesses (Core MANUAL: free text) still carries its limit, in words."""
    if term.operator is None or term.threshold is None:
        return None
    unit = _UNIT_TEXT.get(term.unit or "", "")
    if not unit and currency_of(term.unit):  # money for any country: "LKR_MN" is LKR millions
        unit = " INR crore" if term.unit == "INR_CR" else f" {currency_of(term.unit)} Mn"
    limit = f"{_OPERATOR_TEXT.get(term.operator, term.operator)} {term.threshold:g}{unit}"
    return (f"{term.name or term.metric_raw} must be {limit}. No statement line gives this figure: a person enters it "
            f"each period (agreement page {term.page}).")


def recommendations(db: Session, cp_id: str) -> list[dict]:
    """C1: PUBLISHED proposals from loan agreements actively linked to the CP.

    C7: where a linked agreement amends another, the amendment's term replaces the earlier agreement's
    term for the same covenant (through any chain of amendments); covenants the amendment does not
    mention keep their earlier terms.
    """
    stmt = (select(ExtractedTerm, Document)
            .join(Document, Document.id == ExtractedTerm.document_id)
            .join(DocumentCpLink, DocumentCpLink.document_id == Document.id)
            .where(DocumentCpLink.cp_id == cp_id, DocumentCpLink.unlinked_at.is_(None),
                   Document.type == "LOAN_AGREEMENT", ExtractedTerm.status == "PUBLISHED",
                   ExtractedTerm.category != "INFO")  # FR-29/30 facts are review information, not covenants
            .order_by(ExtractedTerm.created_at))
    rows = db.execute(stmt).all()
    amended_by: dict[str, list[str]] = defaultdict(list)
    for doc in {d.id: d for _, d in rows}.values():
        if doc.amends_document_id:
            amended_by[doc.amends_document_id].append(doc.id)
    keys_by_doc: dict[str, set[str]] = defaultdict(set)
    for term, doc in rows:
        keys_by_doc[doc.id].add(_covenant_key(term))

    def replaced(doc_id: str, key: str, seen: frozenset[str] = frozenset()) -> bool:
        return any(key in keys_by_doc[later] or replaced(later, key, seen | {doc_id})
                   for later in amended_by.get(doc_id, ()) if later not in seen)

    out = []
    for term, doc in rows:
        if replaced(doc.id, _covenant_key(term)):
            continue
        schedule = json.loads(term.schedule_json) if term.schedule_json else {}
        custom = None
        if term.covenant_master_id is None:
            custom = {"name": term.name or term.metric_raw,
                      "definition": term.expression or _manual_rule(term) or term.quote,
                      "assessmentType": "AUTO" if term.category == "FINANCIAL" and term.expression else "MANUAL"}
        out.append({
            "covenantMasterId": term.covenant_master_id,
            "recommendedExpression": term.expression if term.covenant_master_id else None,
            "confidence": term.confidence,
            "rationale": term.quote,
            "frequency": term.frequency,
            "source": {"documentId": doc.id, "fileId": doc.file_id, "page": term.page, "quote": term.quote},
            "customCovenant": custom,
            # Phase 3b-1: step-down levels (each with its rule and period), the agreement's schedule and cure right
            "levels": [lv for lv in schedule.get("levels") or [] if lv.get("expression")] or None,
            "cron": schedule.get("cron"),
            "cure": schedule.get("cure"),
        })
    return out


def remove_document(db: Session, doc: Document, *, user: str, reason: str, data_source_id: str) -> None:
    """B3: take a wrongly uploaded document out of use without deleting it.

    The document is marked REMOVED and kept for the audit trail. Its statement values stop being served;
    where it had replaced an earlier value for the same CP, line and period, that earlier value is served
    again. Its proposed covenants are withdrawn. Core is told about each changed CP and period, so the
    covenants that used these figures are checked again.
    """
    doc.status = "REMOVED"
    touched: set[tuple[str, date]] = set()
    for row in db.scalars(select(StatementValue).where(StatementValue.document_id == doc.id,
                                                       StatementValue.superseded == false())):
        row.superseded = True
        touched.add((row.cp_id, row.as_of_date))
        db.flush()
        still_active = db.scalar(select(func.count()).select_from(StatementValue).where(
            StatementValue.cp_id == row.cp_id, StatementValue.key == row.key,
            StatementValue.as_of_date == row.as_of_date, StatementValue.superseded == false()))
        if not still_active:
            earlier = db.scalars(
                select(StatementValue)
                .outerjoin(Document, Document.id == StatementValue.document_id)
                .where(StatementValue.cp_id == row.cp_id, StatementValue.key == row.key,
                       StatementValue.as_of_date == row.as_of_date, StatementValue.id != row.id,
                       (StatementValue.document_id.is_(None)) | (Document.status != "REMOVED"))
                .order_by(StatementValue.created_at.desc())).first()
            if earlier is not None:
                earlier.superseded = False
    for term in db.scalars(select(ExtractedTerm).where(ExtractedTerm.document_id == doc.id,
                                                       ExtractedTerm.status != "REJECTED")):
        term.status = "WITHDRAWN"
    for cp_id, as_of in sorted(touched):
        db.add(EventOutbox(type="DATA_EVENT", payload_json=json.dumps({
            "dataSourceId": data_source_id, "entityKey": cp_id, "entityKeyLevel": "CP_ID",
            "asOfDate": as_of.isoformat(), "changedAt": utcnow().isoformat(), "documentId": doc.id})))
    audit(db, user, "REMOVE", "document", doc.id, reason=reason, values=len(touched))


def spread(db: Session, doc: Document) -> list[dict]:
    """C6: the full spread for a statement, section by section, from the values it currently serves.

    A line the reader found has status MAPPED (confidence 1.0, or 0.8 when it was worked out from other
    lines); one a reviewer typed in is CONFIRMED; one with no figure is UNMAPPED.
    """
    from dps.catalog import LINE_ITEMS
    from dps.extraction.statement import SECTIONS

    cps = doc.active_cp_ids()
    rows = list(db.scalars(select(StatementValue).where(
        StatementValue.document_id == doc.id, StatementValue.superseded == false(),
        StatementValue.cp_id == (cps[0] if cps else ""), StatementValue.as_of_date == doc.as_of_date,
        StatementValue.source_type != "COMPARATIVE").order_by(StatementValue.created_at)))
    by_key = {r.key: r for r in rows}
    out = []
    for section, keys in SECTIONS.items():
        for key in keys:
            r = by_key.get(key)
            if r is None:
                status, confidence = "UNMAPPED", 0.0
            elif r.source_type == "USER":
                status, confidence = "CONFIRMED", 1.0
            else:
                status, confidence = "MAPPED", 0.8 if r.derived else 1.0
            if section == "LAST_12_MONTHS" and r is None:
                continue  # no 12-month figure could be built: the lines it needs are listed in docs, not here
            derived_label = (r.note or "worked out from other lines") if r is not None and r.derived else None
            out.append({"section": section, "standardKey": key, "standardLabel": LINE_ITEMS[key],
                        "sourceLabel": derived_label,
                        "value": r.value if r else None, "unit": r.unit if r else None,
                        "cell": r.cell if r else None, "page": r.page if r else None,
                        "confidence": confidence, "status": status,
                        "periodMonths": r.period_months if r else None})
    return out


def correct_spread(db: Session, doc: Document, values: dict[str, float], *, user: str, note: str,
                   data_source_id: str, labels: dict[str, str] | None = None) -> None:
    """C6: store reviewer values for a statement, for every CP it applies to, and tell Core about them.

    ``labels``: the statement wording a reviewer confirmed for a line ("Use this" on a suggestion); DPS
    remembers it for each CP, so the borrower's next statement maps that wording on its own.
    """
    for cp in doc.active_cp_ids():
        for key, wording in (labels or {}).items():
            remember_label(db, cp, wording, key, doc.id, user)
    for cp in doc.active_cp_ids():
        for key, value in values.items():
            for old in db.scalars(select(StatementValue).where(
                    StatementValue.cp_id == cp, StatementValue.key == key, StatementValue.as_of_date == doc.as_of_date,
                    StatementValue.superseded == false())):
                old.superseded = True
            unit = statement_money(db, cp, doc.as_of_date)
            db.add(StatementValue(cp_id=cp, key=key, value=value, unit=unit, currency=currency_of(unit),
                                  as_of_date=doc.as_of_date, source_type="USER", document_id=doc.id, note=note,
                                  entered_by=user))
        db.flush()
        _rederive(db, doc, cp, user)
        db.add(EventOutbox(type="DATA_EVENT", payload_json=json.dumps({
            "dataSourceId": data_source_id, "entityKey": cp, "entityKeyLevel": "CP_ID",
            "asOfDate": doc.as_of_date.isoformat(), "changedAt": utcnow().isoformat(), "documentId": doc.id})))
    audit(db, user, "SPREAD_CORRECTED", "document", doc.id, keys=sorted(values), note=note)


def _rederive(db: Session, doc: Document, cp: str, user: str) -> None:
    """Work the derived figures out again from the corrected inputs.

    Bug hunt (8 November 2026): Total Debt (short- plus long-term debt), Net Debt and EBITDA (EBIT plus
    depreciation) were stored when the statement was read and kept their old value after a reviewer corrected an
    input, so a leverage covenant was tested on a stale total. A figure the statement printed, or a person entered,
    is never replaced; only derived ones are.
    """
    from dps.extraction.statement import LineValue, StatementResult, derive

    current = {v.key: v for v in db.scalars(select(StatementValue).where(
        StatementValue.cp_id == cp, StatementValue.as_of_date == doc.as_of_date, StatementValue.superseded == false(),
        StatementValue.source_type != "COMPARATIVE"))}
    unit = statement_money(db, cp, doc.as_of_date)
    result = StatementResult([LineValue(k, v.value, unit) for k, v in current.items() if not v.derived], money=unit)
    derive(result)
    for d in (x for x in result.values if x.derived):
        old = current.get(d.key)
        if old is not None and (not old.derived or abs(old.value - d.value) < 1e-9):
            continue
        if old is not None:
            old.superseded = True
        db.add(StatementValue(cp_id=cp, key=d.key, value=d.value, unit=unit, currency=currency_of(unit),
                              as_of_date=doc.as_of_date, derived=True, source_type="DOCUMENT", document_id=doc.id,
                              note=f"Recomputed after a correction: {d.label}", entered_by=user))
    db.flush()
    from dps.settings import get_settings

    # Phase 2: the borrower's year end the statement was read with (its review record), else the bank's.
    try:
        month = json.loads(doc.review_json or "{}").get("fiscalYearEndMonth")
    except ValueError:
        month = None
    build_twelve_months(db, doc, cp, month if isinstance(month, int) and 1 <= month <= 12
                        else get_settings().fiscal_year_end_month)


# ---------------------------------------------------------------- 12-month covenants (10 November 2026)
#
# A covenant such as Debt / EBITDA is written on twelve months. An interim statement's EBITDA covers three, six or
# nine: tested as it is, Debt / EBITDA from one quarter is about four times too high (a false breach). The trailing
# twelve months (TTM) are built the way a credit analyst builds them, from one interim statement and the last full
# year: this year to date + last full year - last year to date. The interim prints last year's same period beside
# this one, and often the last full year; otherwise the last full year comes from the borrower's annual statement.


def _months_back(day: date, months: int) -> date:
    import calendar

    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def _month_end(year: int, month: int) -> date:
    import calendar

    return date(year, month, calendar.monthrange(year, month)[1])


def months_covered(row_months: int | None, as_of: date, fy_end_month: int) -> int:
    """The months a profit figure covers: as the statement says, else counted from the financial year's start."""
    if row_months:
        return row_months
    return (as_of.month - fy_end_month - 1) % 12 + 1


def store_comparatives(db: Session, doc: Document, cp: str, prior: list, annual: list, annual_end: str,
                       months: int | None) -> None:
    """Last year's same-period column and the last full-year column, as inputs to the 12-month figures.

    They are kept as COMPARATIVE rows: never served to Core, and never in place of a figure the period's own
    statement gave (that statement, uploaded before or after, wins).
    """
    targets = []
    if prior and doc.as_of_date is not None:
        targets.append((prior, _months_back(doc.as_of_date, 12), months, "last year's same period"))
    if annual and annual_end:
        y, m = (int(x) for x in annual_end.split("-"))
        targets.append((annual, _month_end(y, m), 12, "the last full year"))
    for values, as_of, period, what in targets:
        existing = {r.key: r for r in db.scalars(select(StatementValue).where(
            StatementValue.cp_id == cp, StatementValue.as_of_date == as_of, StatementValue.superseded == false(),
            StatementValue.key.in_([v.key for v in values])))}
        for v in values:
            old = existing.get(v.key)
            if old is not None and old.source_type != "COMPARATIVE":
                continue
            if old is not None:
                if old.document_id == doc.id:
                    db.delete(old)
                else:
                    old.superseded = True
            db.add(StatementValue(cp_id=cp, key=v.key, value=v.value, unit=v.unit,
                                  currency=currency_of(v.unit) or "INR", as_of_date=as_of,
                                  period_months=period, derived=v.derived, source_type="COMPARATIVE",
                                  document_id=doc.id, page=v.page,
                                  note=f"Comparative column ({what}) of the statement to {doc.as_of_date}"))
    db.flush()


def build_twelve_months(db: Session, doc: Document, cp: str, fy_end_month: int) -> list[StatementValue]:
    """<KEY>_TTM (trailing twelve months) and <KEY>_ANN (annualised) for this statement's period."""
    from dps.extraction.statement import TTM_KEYS

    as_of = doc.as_of_date
    if as_of is None:
        return []
    keys = list(TTM_KEYS)
    rows = list(db.scalars(select(StatementValue).where(
        StatementValue.cp_id == cp, StatementValue.key.in_(keys), StatementValue.superseded == false(),
        StatementValue.as_of_date <= as_of, StatementValue.as_of_date > _months_back(as_of, 13))))
    year_ago = _months_back(as_of, 12)
    made: list[StatementValue] = []
    for key in keys:
        own = [r for r in rows if r.key == key and r.as_of_date == as_of and r.source_type != "COMPARATIVE"]
        if not own:
            continue
        current = own[-1]
        months = months_covered(current.period_months, as_of, fy_end_month)
        made_for = []
        if months == 12:
            made_for.append(("_TTM", current.value, f"12 months to {as_of}: the figure as reported"))
        else:
            # Only figures in the current one's money unit are added to it: an earlier period kept in another unit
            # (INR crore before the loan's currency was known) gives no 12 months, never a sum of two currencies.
            full = [r for r in rows if r.key == key and year_ago < r.as_of_date < as_of and r.unit == current.unit
                    and months_covered(r.period_months, r.as_of_date, fy_end_month) == 12]
            before = [r for r in rows if r.key == key and abs((r.as_of_date - year_ago).days) <= 7
                      and r.unit == current.unit
                      and months_covered(r.period_months, r.as_of_date, fy_end_month) == months]
            if full and before:
                year = max(full, key=lambda r: r.as_of_date)
                last = before[-1]
                made_for.append(("_TTM", current.value + year.value - last.value,
                                 f"12 months to {as_of}: {months} months to {as_of} + year to {year.as_of_date}"
                                 f" - {months} months to {last.as_of_date}"))
        made_for.append(("_ANN", current.value * 12 / months,
                         f"{months} months to {as_of} x 12/{months} (annualised)"))
        for suffix, value, note in made_for:
            made.append(_put_derived(db, doc, cp, key + suffix, value, note))
    db.flush()
    return made


def _put_derived(db: Session, doc: Document, cp: str, key: str, value: float, note: str) -> StatementValue:
    for old in db.scalars(select(StatementValue).where(
            StatementValue.cp_id == cp, StatementValue.key == key, StatementValue.as_of_date == doc.as_of_date,
            StatementValue.superseded == false())):
        if old.document_id == doc.id and old.derived:
            db.delete(old)
        else:
            old.superseded = True
    unit = statement_money(db, cp, doc.as_of_date)
    row = StatementValue(cp_id=cp, key=key, value=round(value, 6), unit=unit, currency=currency_of(unit),
                         as_of_date=doc.as_of_date, period_months=12, derived=True, source_type="DOCUMENT",
                         document_id=doc.id, note=note)
    db.add(row)
    return row


def remember_label(db: Session, cp_id: str, wording: str, key: str, document_id: str | None, user: str) -> None:
    """Borrower memory: this CP's statements print ``wording`` for ``key``. A newer confirmation replaces an older."""
    from dps.extraction.similarity import canonical

    label = canonical(wording)[:300]
    if not label:
        return
    row = db.scalar(select(LabelMemory).where(LabelMemory.cp_id == cp_id, LabelMemory.label == label))
    if row is None:
        db.add(LabelMemory(cp_id=cp_id, label=label, key=key, source_label=wording[:300], document_id=document_id,
                           confirmed_by=user))
    else:
        row.key, row.source_label, row.document_id, row.confirmed_by = key, wording[:300], document_id, user
    db.flush()
    audit(db, user, "LABEL_REMEMBERED", "cp", cp_id, label=wording[:300], key=key)


def label_memory(db: Session, cp_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Remembered wordings for these CPs: canonical label -> (key, wording as confirmed)."""
    if not cp_ids:
        return {}
    rows = db.scalars(select(LabelMemory).where(LabelMemory.cp_id.in_(cp_ids)).order_by(LabelMemory.created_at))
    return {r.label: (r.key, r.source_label) for r in rows}


class MixedUnits(ValueError):
    """A manual amount in another money unit than the CP's other figures for the period."""


def store_manual_value(db: Session, *, cp_id: str, key: str, value: float, unit: str, currency: str | None,
                       as_of: date, note: str, user: str, data_source_id: str) -> StatementValue:
    """FR-20: approver-entered value. Supersedes the current value for the same key/period and queues C5.

    Money for any country: an amount is entered in the money unit of the CP's other figures for the period (an LKR
    loan's in LKR million); another unit would mix currencies in one ratio and is refused (``MixedUnits``)."""
    if currency_of(unit):
        kept = statement_money(db, cp_id, as_of)
        has_figures = db.scalar(select(StatementValue.id).where(
            StatementValue.cp_id == cp_id, StatementValue.as_of_date == as_of, StatementValue.superseded == false(),
            StatementValue.unit == kept).limit(1)) is not None
        if has_figures and unit != kept:
            raise MixedUnits(f"The figures of {cp_id} for {as_of.isoformat()} are in {kept}; enter this one in {kept}")
        currency = currency_of(unit)
    for old in db.scalars(select(StatementValue).where(
            StatementValue.cp_id == cp_id, StatementValue.key == key,
            StatementValue.as_of_date == as_of, StatementValue.superseded == false())):
        old.superseded = True
    row = StatementValue(cp_id=cp_id, key=key, value=value, unit=unit, currency=currency, as_of_date=as_of,
                         source_type="USER", note=note, entered_by=user)
    db.add(row)
    db.flush()
    db.add(EventOutbox(type="DATA_EVENT", payload_json=json.dumps({
        "dataSourceId": data_source_id, "entityKey": cp_id, "entityKeyLevel": "CP_ID",
        "asOfDate": as_of.isoformat(), "changedAt": row.created_at.isoformat(), "documentId": None})))
    audit(db, user, "MANUAL_VALUE", "statement_value", row.id, key=key, value=value, as_of=as_of, note=note)
    return row
